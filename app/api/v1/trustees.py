from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select

from app.core.constants import MemberStatus, RoleName, resolve_role_name
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.member import Member
from app.models.trustee import Trustee
from app.repositories.member_repository import (
    MEMBER_PROFILE_LOADS,
    TRUSTEE_ROLES,
    MemberRepository,
    designation_filter,
    member_group_filter,
    trustee_role_filter,
)
from app.repositories.trustee_repository import TrusteeRepository
from app.schemas.trustee import (
    MemberTrusteeOut,
    TrusteeCreate,
    TrusteeOut,
    TrusteeUpdate,
)
from app.services.profile_service import ProfileService

router = APIRouter(prefix="/trustees", tags=["Trustees"])

READ = Depends(require_permission(Permission.TRUSTEE_READ))
CREATE = Depends(require_permission(Permission.TRUSTEE_CREATE))
UPDATE = Depends(require_permission(Permission.TRUSTEE_UPDATE))
DELETE = Depends(require_permission(Permission.TRUSTEE_DELETE))


async def _trustee_listing(
    db: DBSession,
    current_user: CurrentUser,
    *,
    group_id: UUID | None = None,
    is_current: bool | None = None,
    role: str | None = None,
    designation: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """The trustees/post holders of the directory, derived from the members.

    A member qualifies when their linked login account carries ``GROUP_ADMIN``
    or ``SUPER_ADMIN``, or when their group designation is one of the recognised
    post-holder offices (President, Secretary, Treasurer, ...). The member's own
    group decides which group's post holder they are, so a SUPER_ADMIN is a post
    holder of the group they were added to, not of every group. Nothing is
    duplicated: the item is the member, and a member that loses the role or the
    designation simply drops out of the listing.
    """
    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")

    resolved_role: RoleName | None = None
    if (role or "").strip():
        resolved_role = resolve_role_name(role)
        if resolved_role not in TRUSTEE_ROLES:
            raise BadRequestException(
                "Role must be one of: GROUP_ADMIN, SUPER_ADMIN"
            )

    stmt = (
        select(Member)
        .where(Member.is_deleted.is_(False))
        .options(*MEMBER_PROFILE_LOADS)
    )
    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # The trustees of a group are read group-scoped, exactly like the board:
    # a GROUP_ADMIN sees only the trustees of its own group.
    # effective_group = group_id if scope.unrestricted else scope.group_id
    # if effective_group is not None:
    #     stmt = stmt.where(member_group_filter(effective_group))
    # if scope.member_id is not None and scope.group_id is None:
    #     stmt = stmt.where(Member.id == scope.member_id)

    # All roles see all trustees/post holders (Super Admin view for everyone).
    # Only filter by group_id when explicitly passed as a query param.
    if group_id is not None:
        stmt = stmt.where(member_group_filter(group_id))
    if is_current is not None:
        # A current trustee is an approved member; the rest are still pending.
        current = MemberStatus.APPROVED.value
        stmt = stmt.where(
            Member.membership_status == current
            if is_current
            else Member.membership_status != current
        )
    stmt = stmt.where(trustee_role_filter(resolved_role))
    if (designation or "").strip():
        stmt = stmt.where(designation_filter(designation.strip()))
    if query:
        needle = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                Member.first_name.ilike(needle),
                Member.last_name.ilike(needle),
                Member.membership_number.ilike(needle),
                Member.profile_data["group_designation"].astext.ilike(needle),
            )
        )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    profiles = ProfileService(db)
    return paginated(
        "Trustees fetched successfully",
        [_member_trustee_out(i, profiles) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("", dependencies=[READ])
async def list_trustees(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    is_current: bool | None = Query(
        default=None,
        description="Only the current trustees (the approved members) or only "
        "the ones that are not approved yet.",
    ),
    role: str | None = Query(
        default=None,
        max_length=50,
        description="Only the trustees whose account carries this role: "
        "GROUP_ADMIN or SUPER_ADMIN.",
    ),
    designation: str | None = Query(
        default=None,
        max_length=255,
        description="Only the trustees holding this exact designation "
        "(case-insensitive).",
    ),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """The trustees/post holders of the directory.

    A member qualifies when their linked login account carries ``GROUP_ADMIN``
    or ``SUPER_ADMIN``, or when their group designation is one of the recognised
    post-holder offices (President, Secretary, Treasurer, ...). The member's own
    group decides which group's post holder they are, so a SUPER_ADMIN is a post
    holder of the group they were added to, not of every group.

    The board seats kept in the ``trustees`` table are still served by
    ``GET /trustees/board``.
    """
    return await _trustee_listing(
        db,
        current_user,
        group_id=group_id,
        is_current=is_current,
        role=role,
        designation=designation,
        query=query,
        page=page,
        page_size=page_size,
    )


@router.get("/directory", dependencies=[READ])
async def list_member_trustees(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    is_current: bool | None = Query(default=None),
    role: str | None = Query(
        default=None,
        max_length=50,
        description="Only the trustees whose account carries this role: "
        "GROUP_ADMIN or SUPER_ADMIN.",
    ),
    designation: str | None = Query(
        default=None,
        max_length=255,
        description="Only the trustees holding this exact designation "
        "(case-insensitive).",
    ),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """The same trustee listing as ``GET /trustees``, under its own name."""
    return await _trustee_listing(
        db,
        current_user,
        group_id=group_id,
        is_current=is_current,
        role=role,
        designation=designation,
        query=query,
        page=page,
        page_size=page_size,
    )


@router.get("/board", dependencies=[READ])
async def list_board_seats(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    is_current: bool | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """The board seats recorded in the ``trustees`` table.

    A seat is the board's own record of a trustee: it carries the term, the
    order and the seat's designation. The directory of the trustees of the
    community is ``GET /trustees``; this is the list of the seats themselves.
    """
    repo = TrusteeRepository(db)
    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")
    stmt = select(Trustee)
    filters: dict[str, Any] = {
        k: v for k, v in {"is_current": is_current}.items() if v is not None
    }

    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # if scope.unrestricted:
    #     if group_id is not None:
    #         filters["group_id"] = group_id
    # else:
    #     # Trustees are always read group-scoped: the board belongs to a group.
    #     filters["group_id"] = scope.group_id

    # All roles see all board seats. Only filter by group_id when explicitly passed.
    if group_id is not None:
        filters["group_id"] = group_id
    stmt = repo.apply_filters(stmt, filters)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Trustee.sort_order)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Board seats fetched successfully",
        [TrusteeOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


def _member_trustee_role(member: Member) -> str | None:
    """The administrator tag of a member: ``GROUP_ADMIN`` or ``SUPER_ADMIN``.

    A post holder designated only by office (for example ``President``) has no
    administrator role; its tag is ``None`` and the designation is shown instead.
    """
    user = member.user
    if user is None or user.role is None:
        return None
    resolved = resolve_role_name(user.role.name)
    return resolved.value if resolved in TRUSTEE_ROLES else None


def _member_trustee_out(member: Member, profiles: ProfileService) -> MemberTrusteeOut:
    """The trustee view of a member, built from the member's own fields.

    The designation is read through the profile view, so the free-text value in
    ``profile_data`` and its mirror in the professional record are treated the
    same way, and a designation that is only whitespace never reaches the
    listing.
    """
    profile = profiles.build_profile(member)
    group = member.group
    if group is not None and group.is_deleted:
        group = None
    role = _member_trustee_role(member)
    designation = (profile.group_designation or "").strip()
    return MemberTrusteeOut(
        id=member.id,
        member_id=member.id,
        group_id=member.group_id,
        group_name=group.name if group else None,
        first_name=member.first_name,
        last_name=member.last_name,
        full_name=profile.full_name or "",
        role=role,
        # The tag is the role when the member has no board designation to show.
        designation=designation or (role or ""),
        contact_email=member.contact_email,
        contact_phone=member.contact_phone,
        profile_photo_url=member.profile_photo_url,
        city=profile.city,
        membership_status=member.membership_status,
        membership_number=member.membership_number,
        joined_at=member.joined_at,
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


@router.post("", dependencies=[CREATE])
async def create_trustee(body: TrusteeCreate, current_user: CurrentUser, db: DBSession):
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    if data["group_id"] is None:
        raise BadRequestException("A trustee must belong to a group")

    # A linked member must belong to the same group as the board, otherwise the
    # trustee record and the member profile would disagree about whose group this is.
    if body.member_id is not None:
        member = await MemberRepository(db).get_by_id(body.member_id)
        if member is None:
            raise NotFoundException("Member", str(body.member_id))
        if member.group_id != data["group_id"]:
            raise BadRequestException(
                "That member does not belong to this group"
            )
        if not data["first_name"]:
            data["first_name"] = member.first_name
        if not data["last_name"]:
            data["last_name"] = member.last_name
        data["profile_photo_url"] = (
            data["profile_photo_url"] or member.profile_photo_url
        )

    trustee = await TrusteeRepository(db).create(**data)
    # Keep the seat's designation mirrored on its member, so the member profile
    # and the board seat agree even though the trustee listing is role-based.
    await _mirror_designation_to_member(
        db, member_id=trustee.member_id, designation=trustee.designation
    )
    await db.flush()
    return created("Trustee created successfully", TrusteeOut.model_validate(trustee))


@router.get("/{trustee_id}", dependencies=[READ])
async def get_trustee(trustee_id: UUID, db: DBSession, current_user: CurrentUser):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    scope = await get_access_scope(db, current_user)
    # Reported as missing rather than forbidden so a cross-group id is not
    # confirmed to exist.
    if not scope.unrestricted and trustee.group_id != scope.group_id:
        raise NotFoundException("Trustee", str(trustee_id))
    return ok("Trustee fetched successfully", TrusteeOut.model_validate(trustee))


@router.patch("/{trustee_id}", dependencies=[UPDATE])
async def update_trustee(
    trustee_id: UUID, body: TrusteeUpdate, current_user: CurrentUser, db: DBSession
):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    await assert_group_access(db, current_user, trustee.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)
    trustee = await repo.update(trustee, **body.model_dump(exclude_unset=True))
    # The seat and the member stay in step: the designation of the seat is also
    # written onto the member profile.
    await _mirror_designation_to_member(
        db, member_id=trustee.member_id, designation=trustee.designation
    )
    return ok("Trustee updated successfully", TrusteeOut.model_validate(trustee))


@router.delete("/{trustee_id}", dependencies=[DELETE])
async def delete_trustee(
    trustee_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    await assert_group_access(db, current_user, trustee.group_id)
    # The seat carried this designation on the member, so retiring the seat
    # clears it - unless the member has since been given another one.
    await _mirror_designation_to_member(
        db,
        member_id=trustee.member_id,
        designation=None,
        only_if_current=trustee.designation,
    )
    await repo.delete(trustee)
    return ok("Trustee deleted successfully")


async def _mirror_designation_to_member(
    db: DBSession,
    *,
    member_id: UUID | None,
    designation: str | None,
    only_if_current: str | None = None,
) -> None:
    """Write a board seat's designation onto the member it belongs to.

    The seat and the member profile must agree about the seat's designation.
    ``only_if_current`` leaves a designation that the member has since changed
    alone.
    """
    if member_id is None:
        return
    member = await MemberRepository(db).get_by_id(member_id)
    if member is None:
        return
    profiles = ProfileService(db)
    current = _member_designation(member)
    if only_if_current is not None and (
        (current or "").strip() != only_if_current.strip()
    ):
        return
    await profiles.apply_values(member, {"group_designation": designation})


def _member_designation(member: Member) -> str | None:
    """The designation a member currently carries.

    The designation is kept in ``profile_data`` and mirrored into the
    professional record; either may hold it, so both are read.
    """
    stored = (member.profile_data or {}).get("group_designation")
    if isinstance(stored, str) and stored.strip():
        return stored
    for professional in member.professional_info:
        designation = professional.designation
        if designation and designation.strip():
            return designation
    return stored if isinstance(stored, str) else None
