from __future__ import annotations

import asyncio
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.constants import (
    DEFAULT_USER_PASSWORD,
    AuditAction,
    MemberStatus,
    RoleName,
    resolve_role_name,
)
from app.core.exceptions import (
    BadRequestException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    get_admin_scope,
    member_in_group,
)
from app.core.security import hash_password
from app.dependencies import CurrentUser, DBSession, OptionalUser, require_permission
from app.models.group import GroupMemberCounter, SocialGroup
from app.models.member import Member
from app.models.user import Role, User
from app.repositories.group_repository import GroupRepository
from app.repositories.member_repository import (
    MEMBER_PROFILE_LOADS,
    MemberRepository,
    member_group_filter,
)
from app.repositories.user_repository import UserRepository
from app.schemas.group import (
    GroupAdminAssignment,
    GroupAdminOut,
    GroupMemberAssignment,
    GroupMemberOut,
    SocialGroupCreate,
    SocialGroupOut,
    SocialGroupUpdate,
)
from app.services import member_group_service as memberships
from app.services.audit_service import AuditService
from app.services.group_id_service import (
    _is_membership_number_violation,
    next_group_id,
    resolve_city,
)
from app.services.sheet_groups_service import read_sheet_groups
from app.utils.helpers import split_full_name
from app.utils.validators import normalize_phone_number

router = APIRouter(prefix="/groups", tags=["Groups"])

#: Unique constraint backing ``SocialGroup.group_id`` (see the model/migration).
UNIQUE_GROUP_ID = "uq_social_groups_group_id"

READ = Depends(require_permission(Permission.GROUP_READ))
CREATE = Depends(require_permission(Permission.GROUP_CREATE))
UPDATE = Depends(require_permission(Permission.GROUP_UPDATE))
DELETE = Depends(require_permission(Permission.GROUP_DELETE))
# A GROUP_ADMIN must be able to add and remove members of its own group, which is
# why membership management is not folded into GROUP_UPDATE (a group-settings edit).
MANAGE_MEMBERS = Depends(require_permission(Permission.GROUP_MEMBER_MANAGE))
ASSIGN_ADMIN = Depends(require_permission(Permission.ROLE_MANAGE))


async def _resolve_role(db, role_name: str | None) -> Role:
    """Resolve a role label to its ``roles`` row.

    ``resolve_role_name`` accepts the retired labels and folds them onto a
    surviving role, so a stale client that still sends "ADMIN" grants
    SUPER_ADMIN rather than a role that no longer exists.
    """
    canonical = resolve_role_name(role_name)
    if canonical is None:
        raise BadRequestException(f"Role '{role_name}' does not exist")
    result = await db.execute(select(Role).where(Role.name == canonical.value))
    role = result.scalar_one_or_none()
    if role is None:
        raise BadRequestException(f"Role '{canonical.value}' is not configured yet")
    return role


async def _get_user_by_phone_forms(
    repo: UserRepository, normalized: str
) -> User | None:
    """Find an account stored either as ``+91XXXXXXXXXX`` or ``XXXXXXXXXX``."""
    user = await repo.get_by_phone(normalized)
    if user is None:
        local = normalized.removeprefix("+91")
        if local != normalized:
            user = await repo.get_by_phone(local)
    return user


async def _assign_default_group_admin(
    db,
    group: SocialGroup,
    admin_name: str,
    admin_phone_number: str,
    admin_password: str | None = None,
) -> User:
    """Create or promote the admin account and link its member record.

    When an account with this phone number already exists, it is promoted to
    GROUP_ADMIN and linked to the group. Otherwise, a new account is created.
    The admin receives role GROUP_ADMIN, member status APPROVED, and a sequential
    member ID for this group.
    """
    role = await _resolve_role(db, RoleName.GROUP_ADMIN.value)
    normalized = normalize_phone_number(admin_phone_number)
    user_repo = UserRepository(db)
    member_repo = MemberRepository(db)

    user = await _get_user_by_phone_forms(user_repo, normalized)
    is_new_user = user is None
    if user is None:
        raw_password = (
            admin_password.strip()
            if admin_password and admin_password.strip()
            else DEFAULT_USER_PASSWORD
        )
        user = await user_repo.create(
            phone_number=normalized,
            full_name=admin_name.strip(),
            password_hash=hash_password(raw_password),
            is_active=True,
            role_id=role.id,
            group_id=group.id,
        )
    else:
        user.role_id = role.id
        user.group_id = group.id
        if admin_password and admin_password.strip():
            user.password_hash = hash_password(admin_password.strip())
        await db.flush()
        await db.refresh(user)

    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={
            "role": role.name,
            "group_id": str(group.id),
            "source": "group_create",
            "created": is_new_user,
        },
    )

    member = await member_repo.get_by_user_id(user.id)
    if member is None and user.phone_number:
        member = await member_repo.get_by_phone(user.phone_number)

    if member is None:
        first_name, last_name = split_full_name(user.full_name)
        member = await member_repo.create(
            user_id=user.id,
            first_name=first_name,
            last_name=last_name or "",
            contact_phone=user.phone_number or normalized,
            membership_status=MemberStatus.APPROVED.value,
            is_profile_complete=False,
            profile_data={
                "full_name": user.full_name,
                "contact_phone": user.phone_number or normalized,
                "social_group_name": group.name,
                "city": group.city,
                "state": group.state,
            },
        )
    else:
        member.user_id = user.id
        member.membership_status = MemberStatus.APPROVED.value

    await memberships.add_membership(db, member, group)
    await AuditService(db).log(
        AuditAction.MEMBER_CREATE if is_new_user else AuditAction.GROUP_UPDATE,
        entity_type="member",
        entity_id=member.id,
        details={
            "group_id": str(group.id),
            "source": "group_create_admin",
            "membership_status": MemberStatus.APPROVED.value,
        },
    )
    return user


@router.get("")
async def list_groups(
    db: DBSession,
    current_user: OptionalUser,
    query: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    source: str = Query(
        default="db",
        pattern="^(db|sheets)$",
        description="db = social_groups table, sheets = distinct names on the "
        "registration Google Sheet",
    ),
    field: str | None = Query(
        default=None,
        pattern="^(social_group_name|group_designation)$",
        description="Sheet column to read; source=sheets only",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Groups, either the stored ``social_groups`` rows or the free-text group
    names read from the registration sheet (``source=sheets``).

    The sheet source answers with ``{"id", "name", "field"}`` items, is
    de-duplicated and sorted by name, and ignores ``status``/paging - it is a
    picker's option list, not a paged resource. This endpoint is public; when
    an authenticated GROUP_ADMIN calls it, the database source still shows only
    its own group.
    """
    if source == "sheets":
        # Reading the sheet is a network call on Google's side, so it runs in a
        # worker thread instead of blocking the event loop.
        sheet_items = await asyncio.to_thread(read_sheet_groups)
        if field is not None:
            sheet_items = [i for i in sheet_items if i["field"] == field]
        if query:
            needle = query.strip().casefold()
            sheet_items = [i for i in sheet_items if needle in i["name"].casefold()]
        return ok("Groups fetched successfully", sheet_items)

    repo = GroupRepository(db)

    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # Anonymous callers see every group. An authenticated GROUP_ADMIN sees only
    # their own group; without this, the search box would leak the rest.
    # visible_group = None
    # if current_user is not None:
    #     scope = await get_access_scope(db, current_user)
    #     visible_group = None if scope.unrestricted else scope.group_id

    # All authenticated roles (Group Admin, Member, Super Admin) see all groups.
    # Only restrict by explicit query param if provided.
    visible_group = None  # no role-based restriction
    if query:
        groups = await repo.search(
            query,
            group_id=visible_group,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return ok(
            "Groups fetched successfully",
            [SocialGroupOut.model_validate(g) for g in groups],
        )
    # A deleted group is soft-deleted (`is_deleted=True`); keep it out of the
    # listing so the panel's group directory reflects the deletion.
    stmt = select(SocialGroup).where(SocialGroup.is_deleted.is_(False))
    filters: dict[str, Any] = {
        k: v for k, v in {"status": status}.items() if v is not None
    }
    if visible_group is not None:
        filters["id"] = visible_group
    stmt = repo.apply_filters(stmt, filters)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(SocialGroup.name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Groups fetched successfully",
        [SocialGroupOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_group(body: SocialGroupCreate, db: DBSession):
    """Create a group, generating its Group ID on the backend.

    ``state``/``city`` must be a configured pair (422 otherwise); the ID is
    ``STATE_CODE + CITY_CODE + SEQUENCE`` for that city - ``MP09001`` for the
    first Indore group. The counter, the ID and the row are written in one
    transaction, and a client-sent ``group_id`` is ignored. An unchanged name
    still returns the existing group instead of a duplicate.

    When ``admin_name`` and ``admin_phone_number`` are supplied together, that
    phone's account is created (or promoted) to this group's ``GROUP_ADMIN`` in
    the same transaction.
    """
    repo = GroupRepository(db)
    city = await resolve_city(db, body.state, body.city)
    existing = await repo.get_by_name(body.name)
    if existing:
        if body.admin_name is not None:
            assert body.admin_phone_number is not None
            await _assign_default_group_admin(
                db,
                existing,
                body.admin_name,
                body.admin_phone_number,
                admin_password=body.admin_password,
            )
            await db.refresh(existing, ["admins"])
        return created(
            "Group created successfully", SocialGroupOut.model_validate(existing)
        )
    # ``group_id`` and the admin fields are dropped from the group row: the
    # server owns the ID, and the account is written separately below.
    values = body.model_dump(
        exclude={"group_id", "admin_name", "admin_phone_number", "admin_password"}
    )
    values["state"] = city.state
    values["city"] = city.city
    try:
        values["group_id"] = await next_group_id(db, city)
        group = await repo.create(**values)
        db.add(GroupMemberCounter(group_uuid=group.id, last_member_seq=0))
        await db.flush()
        if body.admin_name is not None:
            assert body.admin_phone_number is not None
            await _assign_default_group_admin(
                db,
                group,
                body.admin_name,
                body.admin_phone_number,
                admin_password=body.admin_password,
            )
        await db.flush()
    except IntegrityError as exc:
        # The unique index on group_id is the backstop behind the counter: if
        # it ever fires (hand-inserted duplicate, mis-seeded counter) the whole
        # transaction - the claimed sequence included - is rolled back and the
        # caller gets a conflict rather than a duplicate ID.
        await db.rollback()
        if _is_group_id_violation(exc):
            raise ConflictException(
                "Could not allocate a unique Group ID for this city; "
                "retry the request."
            ) from exc
        if _is_membership_number_violation(exc):
            raise ConflictException(
                "Could not allocate a unique member ID for this group; "
                "retry the request."
            ) from exc
        if "phone_number" in str(exc):
            raise ConflictException(
                "This phone number is already registered."
            ) from exc
        raise
    except Exception:
        await db.rollback()
        raise
    if body.admin_name is not None:
        await db.refresh(group, ["admins"])
    return created("Group created successfully", SocialGroupOut.model_validate(group))


def _is_group_id_violation(exc: IntegrityError) -> bool:
    """True when the unique constraint on ``social_groups.group_id`` fired.

    asyncpg exposes the constraint name on the driver error, but SQLAlchemy's
    translated ``IntegrityError`` may carry a wrapper instead - so the message
    is checked as well. Matching on the name keeps a violation of some *other*
    constraint (or a NOT NULL slip) on its usual path.
    """
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    return constraint == UNIQUE_GROUP_ID or UNIQUE_GROUP_ID in str(exc)


@router.get("/{group_id}", dependencies=[READ])
async def get_group(group_id: UUID, db: DBSession, current_user: CurrentUser):
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and scope.group_id != group_id:
        raise NotFoundException("Group", str(group_id))
    return ok("Group fetched successfully", SocialGroupOut.model_validate(group))


@router.get("/{group_id}/members", dependencies=[READ])
async def list_group_members(
    group_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    status: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Members of a group. This is how a Group Admin's directory stays group-bound."""
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and scope.group_id != group_id:
        # Reported as missing rather than forbidden, matching GET /groups/{id}.
        raise NotFoundException("Group", str(group_id))

    stmt = select(Member).where(
        member_group_filter(group_id), Member.is_deleted.is_(False)
    ).options(*MEMBER_PROFILE_LOADS, selectinload(Member.user).selectinload(User.role))
    if status is not None:
        stmt = stmt.where(Member.membership_status == status)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Group members fetched successfully",
        [_group_member_out(m, group_id=group_id) for m in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


def _group_member_out(
    member: Member, group_id: UUID | None = None
) -> GroupMemberOut:
    """``full_name`` lives in ``profile_data`` for user-less members, so fall back."""
    full_name = (member.profile_data or {}).get("full_name")
    if not full_name and member.user is not None:
        full_name = member.user.full_name
    if not full_name:
        full_name = " ".join(
            part
            for part in (member.first_name, member.middle_name, member.last_name)
            if part
        )
    membership_number = member.membership_number
    is_primary = True
    if group_id is not None:
        memberships_list = getattr(member, "group_memberships", None) or []
        matching = next(
            (m for m in memberships_list if m.group_id == group_id),
            None,
        )
        if matching:
            membership_number = matching.membership_number or (
                member.membership_number if matching.is_primary else None
            )
            is_primary = matching.is_primary
        else:
            is_primary = (member.group_id == group_id)

    raw_group = (
        (member.profile_data or {}).get("social_group_association_name")
        or (member.profile_data or {}).get("social_group_name")
    )
    group_name = member.group.name if member.group and not getattr(member.group, "is_deleted", False) else None

    # Build the full groups list (primary + secondary) with names for frontend display.
    memberships_list = sorted(
        getattr(member, "group_memberships", None) or [],
        key=lambda item: item.priority,
    )
    groups = []
    group_memberships_out = []
    for m in memberships_list:
        grp = m.group
        if grp is not None and getattr(grp, "is_deleted", False):
            continue
        gid = (grp.group_id if grp and grp.group_id else None) or str(m.group_id)
        gname = (grp.name if grp else None) or m.group_name or ""
        mnum = m.membership_number or (member.membership_number if m.is_primary else None)
        groups.append({
            "group_id": gid,
            "group_name": gname,
            "membership_number": mnum,
            "is_primary": m.is_primary,
        })
        group_memberships_out.append({
            "id": m.id,
            "group_id": m.group_id,
            "group_name": m.group_name,
            "group_code": m.group_code,
            "membership_number": mnum,
            "priority": m.priority,
            "is_primary": m.is_primary,
            "joined_at": m.joined_at,
        })

    # Fallback: if no membership rows exist, use the legacy group_id pointer.
    if not groups and member.group and not getattr(member.group, "is_deleted", False):
        gid = member.group.group_id or str(member.group.id)
        gname = member.group.name
        groups.append({
            "group_id": gid,
            "group_name": gname,
            "membership_number": member.membership_number,
            "is_primary": True,
        })
        group_memberships_out.append({
            "id": member.id,
            "group_id": member.group.id,
            "group_name": member.group.name,
            "group_code": member.group.group_id,
            "membership_number": member.membership_number,
            "priority": 1,
            "is_primary": True,
            "joined_at": member.joined_at,
        })

    role_name = "MEMBER"
    if member.user is not None and getattr(member.user, "role", None) is not None:
        role_name = member.user.role.name

    return GroupMemberOut(
        id=member.id,
        member_id=member.id,
        user_id=member.user_id,
        full_name=full_name,
        first_name=member.first_name,
        last_name=member.last_name,
        role=role_name,
        role_name=role_name,
        phone_number=member.contact_phone,
        email=member.contact_email,
        membership_number=membership_number,
        is_primary=is_primary,
        membership_status=member.membership_status,
        status=member.membership_status,
        group_id=member.group_id,
        group_name=group_name,
        social_group_name=group_name or raw_group,
        social_group_association_name=raw_group or group_name,
        group_ids=[
            row.group_id
            for row in memberships_list
        ],
        groups=groups,
        group_memberships=group_memberships_out,
        contact_email=member.contact_email,
        contact_phone=member.contact_phone,
        profile_photo_url=member.profile_photo_url,
        created_at=member.created_at,
    )


@router.get("/{group_id}/admins", dependencies=[READ])
async def list_group_admins(
    group_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """List all administrators for a specific social group.

    Only accounts whose actual role is exactly GROUP_ADMIN are returned.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and scope.group_id != group_id:
        raise NotFoundException("Group", str(group_id))

    stmt = (
        select(User)
        .join(Role, Role.id == User.role_id)
        .where(
            User.group_id == group_id,
            User.is_deleted.is_(False),
            func.upper(func.trim(Role.name)) == RoleName.GROUP_ADMIN.value,
        )
        .order_by(User.full_name)
    )
    admins = (await db.execute(stmt)).scalars().all()
    return ok(
        "Group admins fetched successfully",
        [GroupAdminOut.model_validate(a) for a in admins],
    )


@router.post("/{group_id}/admins", dependencies=[ASSIGN_ADMIN])
async def assign_group_admin(group_id: UUID, body: GroupAdminAssignment, db: DBSession):
    """Grant a login account administration of this group.

    Sets ``User.group_id`` (the scope source of truth in ``app/core/scope.py``)
    and assigns the role. Reassigning the same user to the same group is a no-op
    rather than an error, so the panel can retry safely.
    """
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))

    role = await _resolve_role(db, body.role_name)
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(body.user_id)
    if user is None:
        raise NotFoundException("User", str(body.user_id))
    if not user.is_active:
        raise BadRequestException("Cannot assign an inactive account as a Group Admin")

    user.role_id = role.id
    user.group_id = group_id
    await db.flush()
    await db.refresh(user)

    member_repo = MemberRepository(db)
    member = await member_repo.get_by_user_id(user.id)
    if member is None and user.phone_number:
        member = await member_repo.get_by_phone(user.phone_number)
    if member is None:
        first_name, last_name = split_full_name(user.full_name)
        member = await member_repo.create(
            user_id=user.id,
            first_name=first_name,
            last_name=last_name or "",
            contact_phone=user.phone_number,
            contact_email=user.email,
            membership_status=MemberStatus.APPROVED.value,
            is_profile_complete=False,
            profile_data={
                "full_name": user.full_name,
                "contact_phone": user.phone_number,
                "social_group_name": group.name,
                "city": group.city,
                "state": group.state,
            },
        )
    else:
        member.user_id = user.id
        member.membership_status = MemberStatus.APPROVED.value
    await memberships.add_membership(db, member, group)

    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={"role": role.name, "group_id": str(group_id)},
    )
    return ok(
        "Group Admin assigned successfully",
        {
            "group": SocialGroupOut.model_validate(group),
            "admin": GroupAdminOut.model_validate(user),
        },
    )


@router.delete("/{group_id}/admins/{user_id}", dependencies=[ASSIGN_ADMIN])
async def remove_group_admin(group_id: UUID, user_id: UUID, db: DBSession):
    """Revoke a login account's administration of this group.

    Demotes the account to MEMBER and clears ``group_id`` so it loses all group
    scope immediately, rather than leaving a stale scope behind.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    user = await UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    if user.group_id != group_id:
        raise NotFoundException("Group Admin assignment", str(user_id))

    role = await _resolve_role(db, RoleName.MEMBER.value)
    user.role_id = role.id
    user.group_id = None
    await db.flush()
    await db.refresh(user)
    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={"role": RoleName.MEMBER.value, "removed_from_group": str(group_id)},
    )
    return ok("Group Admin removed successfully", GroupAdminOut.model_validate(user))


@router.post("/{group_id}/members", dependencies=[MANAGE_MEMBERS])
async def add_group_member(
    group_id: UUID,
    body: GroupMemberAssignment,
    db: DBSession,
    current_user: CurrentUser,
):
    """Attach an existing member (or a login account) to this group.

    ``member_id`` moves an existing member profile into the group. ``user_id``
    creates the member profile if the account does not have one yet, so a newly
    registered member can be added to a group without a second step.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    # A GROUP_ADMIN may add members to their own group only.
    await assert_group_access(db, current_user, group_id)

    member_repo = MemberRepository(db)
    if body.member_id is not None:
        member = await member_repo.get_by_id(body.member_id)
        if member is None:
            raise NotFoundException("Member", str(body.member_id))
    else:
        assert body.user_id is not None
        member = await member_repo.get_by_user_id(body.user_id)
        if member is None:
            user = await UserRepository(db).get_by_id(body.user_id)
            if user is None:
                raise NotFoundException("User", str(body.user_id))
            name_parts = user.full_name.split()
            member = await member_repo.create(
                user_id=user.id,
                first_name=name_parts[0] if name_parts else user.full_name,
                last_name=name_parts[-1] if len(name_parts) > 1 else "",
                contact_phone=user.phone_number,
                contact_email=user.email,
            )

    scope = await get_admin_scope(db, current_user)
    if (
        scope is not None
        and not await member_in_group(db, member, scope)
        and member.group_id is not None
    ):
        raise ForbiddenException("Operation is limited to your own group")

    _, was_added = await memberships.add_membership(db, member, group)
    if not was_added:
        return ok("Member is already in this group", _group_member_out(member, group_id=group_id))
    await db.refresh(member)
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE,
        entity_type="member",
        entity_id=member.id,
        details={"group_id": str(group_id), "action": "member_added"},
    )
    return created("Member added to group", _group_member_out(member, group_id=group_id))


@router.delete("/{group_id}/members/{member_id}", dependencies=[MANAGE_MEMBERS])
async def remove_group_member(
    group_id: UUID, member_id: UUID, db: DBSession, current_user: CurrentUser
):
    """Remove a member from this group.

    The member profile itself is kept; only the group link is cleared, so no
    family, professional or profile data is destroyed.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group_id)
    member = await MemberRepository(db).get_by_id(member_id)
    if member is None or not await member_in_group(db, member, group_id):
        raise NotFoundException("Member", str(member_id))

    removed, _ = await memberships.remove_membership(db, member, group_id)
    if not removed:
        raise NotFoundException("Member", str(member_id))
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE,
        entity_type="member",
        entity_id=member.id,
        details={"group_id": str(group_id), "action": "member_removed"},
    )
    return ok("Member removed from group")


@router.patch("/{group_id}", dependencies=[UPDATE])
async def update_group(
    group_id: UUID, body: SocialGroupUpdate, current_user: CurrentUser, db: DBSession
):
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)
    updates = body.model_dump(exclude_unset=True)
    if "state" in updates or "city" in updates:
        state = updates.get("state", group.state)
        city = updates.get("city", group.city)
        if not state or not city:
            raise BadRequestException(
                "Both state and city are required when changing a group's location."
            )
        place = await resolve_city(db, state, city)
        updates["state"] = place.state
        updates["city"] = place.city
    group = await repo.update(group, **updates)
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE, entity_type="group", entity_id=group.id
    )
    await db.flush()
    return ok("Group updated successfully", SocialGroupOut.model_validate(group))


@router.delete("/{group_id}", dependencies=[DELETE])
async def delete_group(
    group_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
    force: bool = Query(
        default=False,
        description="Unlink dependent news/fees/trustees/media and delete anyway",
    ),
):
    """Permanently delete a group.

    Unlike most resources in this API a group is **hard-deleted**, so the row
    actually leaves the database. Members, users, advertisements and events are
    unlinked automatically (the FK is ``ON DELETE SET NULL``). News, fees,
    trustees and media would be cascade-deleted, so the request is refused with
    ``409`` while any live row references the group; re-send with ``force=true``
    to unlink those rows and delete the group instead.
    """
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)

    dependents = await repo.count_live_dependents(group.id)
    blocking = {name: count for name, count in dependents.items() if count}
    if blocking and not force:
        raise ConflictException(
            "Group has linked records ("
            + ", ".join(f"{name}: {count}" for name, count in blocking.items())
            + "). Re-send with force=true to unlink them and delete the group."
        )

    group_id_value = group.id
    await repo.hard_delete(group)
    await AuditService(db).log(
        AuditAction.GROUP_DELETE,
        entity_type="group",
        entity_id=group_id_value,
        details={"hard_delete": True, "force": force, "unlinked": blocking},
    )
    return ok("Group deleted successfully")
