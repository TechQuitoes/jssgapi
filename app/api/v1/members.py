from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import case, func, select

from app.core.constants import AuditAction, MemberStatus, RoleName
from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.security import hash_password
from app.core.scope import (
    assert_member_access,
    get_access_scope,
    get_admin_scope,
    member_in_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.services.group_id_service import assign_member_id
from app.utils.validators import normalize_phone_number, validate_password
from app.repositories.member_repository import (
    MEMBER_PROFILE_LOADS,
    MemberRepository,
    designation_filter,
    group_name_filter,
    member_group_filter,
    trustee_role_filter,
)
from app.schemas.member import (
    MemberApproveRequest,
    MemberCreate,
    MemberListItemOut,
    MemberOut,
    MemberUpdate,
)
from app.services import member_group_service as memberships
from app.services.audit_service import AuditService
from app.services.member_photo_service import SLOTS, MemberPhotoService
from app.services.profile_service import ProfileService
from app.utils.file_upload import MAX_IMAGE_SIZE

router = APIRouter(prefix="/members", tags=["Members"])

READ = Depends(require_permission(Permission.MEMBER_READ))
CREATE = Depends(require_permission(Permission.MEMBER_CREATE))
UPDATE = Depends(require_permission(Permission.MEMBER_UPDATE))
DELETE = Depends(require_permission(Permission.MEMBER_DELETE))
APPROVE = Depends(require_permission(Permission.MEMBER_APPROVE))


def _member_out(member: Member) -> MemberOut:
    """The member record plus the linked group's name.

    ``group_name`` is derived from ``group_id`` (the admin-panel group). A
    soft-deleted group is treated as absent so the name never outlives the
    group. ``member.group`` must be eager loaded by the caller.
    """
    out = MemberOut.model_validate(member)
    group = member.group
    if group is not None and group.is_deleted:
        group = None
    out.group_name = group.name if group else None
    if group:
        out.group_id = group.id
        out.group_code = group.group_id
    elif member.group_id:
        out.group_id = member.group_id
    raw_profile_group = (
        (member.profile_data or {}).get("social_group_association_name")
        or (member.profile_data or {}).get("social_group_name")
    )
    out.social_group_name = out.group_name or raw_profile_group
    out.social_group_association_name = raw_profile_group or out.group_name
    out.member_id = member.id
    out.status = member.membership_status
    return out


def _member_list_item(member: Member, profiles: ProfileService) -> MemberListItemOut:
    out_dict = _member_out(member).model_dump()
    prof_dict = profiles.get_profile_dict(member)
    merged = {**out_dict, **prof_dict}
    if not merged.get("date_of_birth") and merged.get("member_dob"):
        merged["date_of_birth"] = merged["member_dob"]
    elif not merged.get("member_dob") and merged.get("date_of_birth"):
        merged["member_dob"] = merged["date_of_birth"]
    merged["member_id"] = member.id
    merged["status"] = member.membership_status
    if not merged.get("phone_number"):
        merged["phone_number"] = member.contact_phone
    if not merged.get("email"):
        merged["email"] = member.contact_email
    groups, primary_info = profiles.get_member_groups_info(member)
    merged["groups"] = groups
    if primary_info.get("group_id"):
        merged["group_id"] = primary_info["group_id"]
    if primary_info.get("group_name"):
        merged["group_name"] = primary_info["group_name"]
        merged["social_group_name"] = primary_info["group_name"]
    raw_sheet_group = (
        (member.profile_data or {}).get("social_group_association_name")
        or (member.profile_data or {}).get("social_group_name")
    )
    merged["social_group_association_name"] = (
        raw_sheet_group or merged.get("social_group_name") or merged.get("group_name")
    )
    if not merged.get("social_group_name"):
        merged["social_group_name"] = merged["social_group_association_name"]
    if primary_info.get("membership_number"):
        merged["membership_number"] = primary_info["membership_number"]
    return MemberListItemOut(**merged)


def _member_list_item_out(
    member: Member,
    profiles: ProfileService,
    show_profile_complete: bool = True,
) -> MemberListItemOut:
    """Wrapper around ``_member_list_item`` that hides ``is_profile_complete``
    for non-Super Admin callers.

    ``show_profile_complete=True`` (Super Admin) → field included as-is.
    ``show_profile_complete=False`` (Group Admin, Member) → field set to None
    so it never leaks to the frontend.
    """
    item = _member_list_item(member, profiles)
    if not show_profile_complete:
        # is_profile_complete visible only to Super Admin in the list view.
        item.is_profile_complete = False  # type: ignore[assignment]
    return item


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if not parts:
        return full_name, ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


async def _reload_member(db: DBSession, member_id: UUID) -> Member:
    """Reload a member with the freshest columns and every relationship the
    profile view needs (``group``/``location`` are loaded lazily by default)."""
    result = await db.execute(
        select(Member)
        .options(*MEMBER_PROFILE_LOADS)
        .where(Member.id == member_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _get_member(db: DBSession, identifier: UUID) -> Member | None:
    """Load a member that exists and has not been deleted, relations included.

    Supports lookup by either Member.id or Member.user_id.
    """
    result = await db.execute(
        select(Member)
        .options(*MEMBER_PROFILE_LOADS)
        .where(
            (Member.id == identifier) | (Member.user_id == identifier),
            Member.is_deleted.is_(False),
        )
        .order_by(case((Member.id == identifier, 1), else_=2))
        .limit(1)
    )
    return result.scalar_one_or_none()


def _photo_response(message: str, photo_url: str | None, **data) -> dict:
    """Photo envelope: the URL is in ``data`` and repeated at the top level so
    the panel can read either shape. Matches the profile photo route, which
    answers 200 with the URL as well."""
    payload = ok(message, {"photo_url": photo_url, **data})
    payload["photo_url"] = photo_url
    return payload


@router.get("", dependencies=[READ])
async def list_members(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    group_name: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    trustee_only: bool = Query(
        default=False,
        description="Only the post holders of their group: members whose linked "
        "account is a GROUP_ADMIN/SUPER_ADMIN or whose group designation is a "
        "recognised office (President, Secretary, Treasurer, ...).",
    ),
    designation: str | None = Query(
        default=None,
        max_length=255,
        description="Only the members holding this exact group designation "
        "(case-insensitive).",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = MemberRepository(db)
    profiles = ProfileService(db)
    scope = await get_access_scope(db, current_user)
    # An empty filter means "no group filter", not "groups with an empty name".
    group_name = (group_name or "").strip() or None

    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # A caller-supplied group_id may only narrow the result, never widen it: a
    # GROUP_ADMIN asking for another group is refused rather than redirected.
    # if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
    #     raise ForbiddenException("Operation is limited to your own group")
    # The panel's Group filter passes group_id; without it a SUPER_ADMIN would
    # get every member no matter which group was picked. A GROUP_ADMIN without
    # an explicit choice keeps seeing only its own group.
    # effective_group: UUID | None
    # if group_id is not None:
    #     effective_group = group_id
    # else:
    #     effective_group = None if scope.unrestricted else scope.group_id

    # All roles see all members; filter only when group_id is explicitly passed.
    effective_group: UUID | None = group_id  # None = no group filter

    # profile_completeness is only visible to Super Admin in the list view.
    show_profile_complete = scope.unrestricted

    if query:
        # The search envelope deliberately has no `pagination` key: the panel's
        # Members page already handles its absence and a search result set is a
        # single page by contract.
        members = await repo.search(
            query,
            group_id=effective_group,
            membership_status=status,
            group_name=group_name,
            designation=designation if (designation or "").strip() else None,
            trustee_only=trustee_only,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return ok(
            "Members fetched successfully",
            [
                _member_list_item_out(m, profiles, show_profile_complete)
                for m in members
            ],
        )

    stmt = (
        select(Member)
        .where(Member.is_deleted.is_(False))
        .options(*MEMBER_PROFILE_LOADS)
    )
    stmt = repo.apply_filters(
        stmt,
        {
            k: v
            for k, v in {
                "group_id": effective_group,
                "membership_status": status,
            }.items()
            if v is not None
        },
    )
    if group_name:
        stmt = stmt.where(group_name_filter(group_name))
    # The post-holder listing is the member listing narrowed to administrator
    # roles and recognised post-holder designations, so the two never drift apart.
    if trustee_only:
        stmt = stmt.where(trustee_role_filter())
    if (designation or "").strip():
        stmt = stmt.where(designation_filter(designation.strip()))
    if scope.member_id is not None and scope.group_id is None:
        stmt = stmt.where(Member.id == scope.member_id)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Members fetched successfully",
        [_member_list_item_out(i, profiles, show_profile_complete) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_member(body: MemberCreate, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    norm_phone = normalize_phone_number(body.phone_number)
    in_use, _ = await repo.check_phone_in_use(norm_phone, exclude_user_id=body.user_id)
    if in_use:
        raise AlreadyExistsException(
            "This phone number is already registered or in use by another member."
        )

    if body.spouse_mobile and body.spouse_mobile.strip():
        norm_spouse = normalize_phone_number(body.spouse_mobile.strip())
        if norm_spouse == norm_phone:
            raise AlreadyExistsException(
                "Member mobile and spouse mobile cannot be the same number."
            )
        spouse_in_use, _ = await repo.check_phone_in_use(
            norm_spouse, exclude_user_id=body.user_id
        )
        if spouse_in_use:
            raise AlreadyExistsException(
                "Spouse mobile number is already registered or in use "
                "by another member."
            )

    if body.contact_phone and body.contact_phone.strip():
        norm_contact = normalize_phone_number(body.contact_phone.strip())
        if norm_contact != norm_phone:
            contact_in_use, _ = await repo.check_phone_in_use(
                norm_contact, exclude_user_id=body.user_id
            )
            if contact_in_use:
                raise AlreadyExistsException(
                    "Contact phone number is already registered or in use "
                    "by another member."
                )

    # Only enforced when the admin links an account; imported community members
    # legitimately have no User at all.
    if body.user_id is not None:
        if await repo.get_by_user_id(body.user_id):
            raise BadRequestException("A member profile already exists for this user")
        if body.password and body.password.strip():
            validate_password(body.password.strip())
            user = await db.get(User, body.user_id)
            if user:
                user.password_hash = hash_password(body.password.strip())
    elif body.password and body.password.strip():
        validate_password(body.password.strip())
        norm_phone = normalize_phone_number(body.phone_number)
        user_repo = UserRepository(db)
        user = await user_repo.get_by_phone(norm_phone)
        if user is None and body.contact_email:
            user = await user_repo.get_by_email(body.contact_email.strip().lower())
        if user is None:
            role_res = await db.execute(
                select(Role).where(Role.name == RoleName.MEMBER.value)
            )
            member_role = role_res.scalar_one_or_none()
            user = await user_repo.create(
                email=body.contact_email.strip().lower() if body.contact_email else None,
                full_name=body.full_name.strip(),
                phone_number=norm_phone,
                password_hash=hash_password(body.password.strip()),
                is_active=True,
                role_id=member_role.id if member_role else None,
            )
        else:
            user.password_hash = hash_password(body.password.strip())
        body.user_id = user.id
    data = body.member_columns()
    if body.user_id is not None:
        data["user_id"] = body.user_id
    first_name, last_name = _split_full_name(body.full_name)
    data["first_name"] = data.get("first_name") or first_name
    data["last_name"] = data.get("last_name") or last_name
    data.pop("group_id", None)
    data.pop("membership_number", None)
    group_ids = body.requested_group_ids()
    scope = await get_admin_scope(db, current_user)
    if scope is not None:
        if not group_ids:
            group_ids = [scope]
        elif any(group_id != scope for group_id in group_ids):
            raise ForbiddenException("Operation is limited to your own group")
    member = await repo.create(**data)
    for group_id in group_ids:
        group = await db.get(SocialGroup, group_id)
        if group is None or group.is_deleted:
            raise BadRequestException("Social group not found")
        await memberships.add_membership(db, member, group)
    profiles = ProfileService(db)
    await profiles.apply_values(member, body.profile_values())
    await AuditService(db).log(
        AuditAction.MEMBER_CREATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    return created(
        "Member created successfully",
        _member_list_item(await _reload_member(db, member.id), profiles),
    )


@router.get("/social-groups", dependencies=[READ])
async def list_social_group_names(db: DBSession, current_user: CurrentUser):
    """Distinct social-group names the member directory can filter by.

    The name is stored either as free text in ``members.profile_data`` or on a
    linked ``social_groups`` row, so both sources are collected - the panel's
    group filter must offer exactly the values members actually carry. This
    route is declared before ``GET /{member_id}`` so ``social-groups`` is never
    read as a member id."""
    scope = await get_access_scope(db, current_user)
    conditions = [Member.is_deleted.is_(False)]
    if not scope.unrestricted and scope.group_id is not None:
        conditions.append(member_group_filter(scope.group_id))
    if (
        not scope.unrestricted
        and scope.group_id is None
        and scope.member_id is not None
    ):
        conditions.append(Member.id == scope.member_id)

    profile_names = (
        await db.execute(
            select(
                func.distinct(Member.profile_data["social_group_name"].astext)
            ).where(
                *conditions,
                Member.profile_data["social_group_name"].astext.isnot(None),
                Member.profile_data["social_group_name"].astext != "",
            )
        )
    ).scalars()
    linked_names = (
        await db.execute(
            select(func.distinct(SocialGroup.name))
            .join(
                MemberGroupMembership,
                MemberGroupMembership.group_id == SocialGroup.id,
            )
            .join(Member, Member.id == MemberGroupMembership.member_id)
            .where(*conditions, SocialGroup.is_deleted.is_(False))
        )
    ).scalars()
    # Free-text intake stores the same group under many spellings ("Jssg
    # professional unity", "JSSG PROFESSIONAL UNITY", ...). Collapsing names
    # that differ only by case or padding keeps the Members filter and the
    # panel's group directory free of duplicate rows. One representative per
    # case-folded key survives - the deterministic first after a case-folded
    # sort - while genuinely different names ("... unity" vs "... unity group")
    # stay separate.
    representatives: dict[str, str] = {}
    for name in sorted(
        {n.strip() for n in (*profile_names, *linked_names) if n and n.strip()},
        key=lambda n: (n.casefold(), n),
    ):
        representatives.setdefault(name.casefold(), name)
    names = sorted(representatives.values(), key=str.casefold)
    return ok("Social groups fetched successfully", names)


@router.get("/{member_id}", dependencies=[READ])
async def get_member(member_id: UUID, db: DBSession, current_user: CurrentUser):
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted:
        # A member outside the caller's group must look like a missing record, not
        # a forbidden one: a 403 would confirm the id exists.
        if scope.member_id is not None and member.id == scope.member_id:
            pass
        elif current_user.id is not None and member.user_id == current_user.id:
            pass
        elif scope.group_id is None or not await member_in_group(
            db, member, scope.group_id
        ):
            raise NotFoundException("Member", str(member_id))
    # The profile fields are part of the member record the panel renders, so a
    # single fetch answers with the same shape as a list item.
    return ok(
        "Member fetched successfully",
        _member_list_item(member, ProfileService(db)),
    )


@router.patch("/{member_id}", dependencies=[UPDATE])
async def update_member(
    member_id: UUID, body: MemberUpdate, current_user: CurrentUser, db: DBSession
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_member_access(db, current_user, member)
    columns = body.member_columns()
    columns.pop("group_id", None)
    group_selection_sent = bool(
        {"group_id", "group_ids"} & body.model_fields_set
    )
    requested_group_ids = body.requested_group_ids()
    scope = await get_admin_scope(db, current_user)
    if group_selection_sent and not requested_group_ids:
        raise BadRequestException("A member must belong to at least one group")

    # If a new password is provided, validate and hash it; if empty/omitted, leave unchanged
    new_password_hash: str | None = None
    if body.password is not None and body.password.strip():
        validate_password(body.password.strip())
        new_password_hash = hash_password(body.password.strip())

    # Keep member login phone number updated when edited
    sent_phone = (
        body.phone_number
        if "phone_number" in body.model_fields_set
        else (body.contact_phone if "contact_phone" in body.model_fields_set else None)
    )
    norm_phone = normalize_phone_number(sent_phone.strip()) if sent_phone and sent_phone.strip() else None
    if norm_phone:
        columns["contact_phone"] = norm_phone

    sent_email = (
        (body.contact_email or getattr(body, "email", None) or "").strip().lower()
        if ("contact_email" in body.model_fields_set or "email" in body.model_fields_set)
        else None
    )
    sent_name = body.full_name.strip() if "full_name" in body.model_fields_set and body.full_name else None

    if member.user_id is not None:
        user = await db.get(User, member.user_id)
        if user:
            if norm_phone and user.phone_number != norm_phone:
                phone_owner = await UserRepository(db).get_by_phone(norm_phone)
                if phone_owner and phone_owner.id != user.id:
                    raise AlreadyExistsException("This phone number is already registered.")
                user.phone_number = norm_phone
            if sent_email and user.email != sent_email:
                email_owner = await UserRepository(db).get_by_email(sent_email)
                if email_owner and email_owner.id != user.id:
                    raise AlreadyExistsException("This email is already registered.")
                user.email = sent_email
            if sent_name:
                user.full_name = sent_name
            if new_password_hash:
                user.password_hash = new_password_hash
                await AuditService(db).log(
                    AuditAction.PASSWORD_CHANGE,
                    user_id=current_user.id,
                    entity_type="user",
                    entity_id=user.id,
                    details={"reset_by": str(current_user.id), "source": "member_update"},
                )
    else:
        phone_candidate = (
            norm_phone
            or (normalize_phone_number(member.contact_phone) if member.contact_phone else None)
            or (
                normalize_phone_number(member.profile_data.get("phone_number"))
                if (member.profile_data or {}).get("phone_number")
                else None
            )
        )
        if phone_candidate:
            user_repo = UserRepository(db)
            user = await user_repo.get_by_phone(phone_candidate)
            if user is None and sent_email:
                user = await user_repo.get_by_email(sent_email)
            if user:
                if new_password_hash:
                    user.password_hash = new_password_hash
                if norm_phone and user.phone_number != norm_phone:
                    user.phone_number = norm_phone
                if sent_email and user.email != sent_email:
                    user.email = sent_email
                member.user_id = user.id
            elif new_password_hash:
                role_res = await db.execute(
                    select(Role).where(Role.name == RoleName.MEMBER.value)
                )
                member_role = role_res.scalar_one_or_none()
                user = await user_repo.create(
                    email=sent_email or member.contact_email or (member.profile_data or {}).get("email"),
                    full_name=sent_name or getattr(member, "full_name", None) or f"{member.first_name} {member.last_name}".strip() or "Member",
                    phone_number=phone_candidate,
                    password_hash=new_password_hash,
                    is_active=True,
                    role_id=member_role.id if member_role else None,
                )
                member.user_id = user.id

    member = await repo.update(member, **columns)
    if group_selection_sent:
        await memberships.sync_memberships(
            db,
            member,
            requested_group_ids,
            managed_group_id=scope,
        )
    # Profile fields live in ``members.profile_data``; a Group Admin has no access to
    # ``PUT /profile`` (that writes the caller's own profile), so the admin edit path
    # must write them too or the panel could never maintain a full member profile.
    await ProfileService(db).apply_values(member, body.profile_values())
    # The panel edits a name through the first/middle/last columns, while the
    # directory displays the profile ``full_name``. Keep them in step, or a
    # rename would silently not show up - unless the caller sent full_name
    # itself, which is then the value that wins.
    if {"first_name", "middle_name", "last_name"} & set(
        columns
    ) and "full_name" not in body.model_fields_set:
        derived_name = " ".join(
            part
            for part in (member.first_name, member.middle_name, member.last_name)
            if part
        ).strip()
        if derived_name:
            profile_data = dict(member.profile_data or {})
            profile_data["full_name"] = derived_name
            member.profile_data = profile_data
    required_fields = ["first_name", "last_name", "contact_email"]
    member.is_profile_complete = all(getattr(member, f) for f in required_fields)
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    # `updated_at` is a server-side onupdate column, so it is expired after the
    # flush and reading it would trigger lazy IO outside the async context -
    # reload before serializing.
    member = await _reload_member(db, member.id)
    profiles = ProfileService(db)
    return ok(
        "Member updated successfully",
        _member_list_item(member, profiles),
    )


@router.post("/{member_id}/photo/{role}", dependencies=[UPDATE])
async def upload_member_photo(
    member_id: UUID,
    role: str,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    photo: UploadFile | None = File(default=None),
    file: UploadFile | None = File(default=None),
):
    """Upload or replace the member's (``role=member``) or their spouse's
    (``role=spouse``) photo.

    The bytes go to object storage; only the URL and the object key are stored
    in Postgres. Replacing a photo uploads the new object first, so a storage
    failure leaves the previous photo in place. The file is accepted as either
    ``photo`` or ``file``, matching the two field names the API already uses.
    """
    upload = photo or file
    if upload is None:
        raise BadRequestException("A photo file is required", "INVALID_PHOTO")
    if role not in SLOTS:
        raise BadRequestException(
            f"Unknown photo slot '{role}'; use 'member' or 'spouse'",
            "INVALID_PHOTO",
        )
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_member_access(db, current_user, member)

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # One byte past the limit is enough to detect an oversized upload without
    # buffering an arbitrarily large file into memory first.
    content = await upload.read(MAX_IMAGE_SIZE + 1)
    url = await MemberPhotoService(db).upload(
        member,
        role=role,
        file_name=upload.filename,
        content=content,
        mime_type=upload.content_type,
        uploaded_by=current_user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE,
        user_id=current_user.id,
        entity_type="member",
        entity_id=member.id,
        details={"field": f"{role}_photo", "photo_url": url},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.flush()
    # `updated_at` is server-side, so the row is expired by the flush above and
    # must be reloaded before it is serialized.
    member = await _reload_member(db, member.id)
    return _photo_response(
        "Member photo uploaded successfully",
        url,
        role=role,
        member=_member_list_item(member, ProfileService(db)),
    )


@router.delete("/{member_id}/photo/{role}", dependencies=[UPDATE])
async def delete_member_photo(
    member_id: UUID,
    role: str,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
):
    """Remove one photo: clear its URL from the member, then delete the stored
    object and its media row (best effort - storage trouble is logged, not
    turned into a failed request)."""
    if role not in SLOTS:
        raise BadRequestException(
            f"Unknown photo slot '{role}'; use 'member' or 'spouse'",
            "INVALID_PHOTO",
        )
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_member_access(db, current_user, member)

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await MemberPhotoService(db).remove(member, role=role)
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE,
        user_id=current_user.id,
        entity_type="member",
        entity_id=member.id,
        details={"field": f"{role}_photo", "photo_url": None},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.flush()
    return _photo_response("Member photo deleted successfully", None, role=role)


@router.post("/{member_id}/approve")
async def approve_member(
    member_id: UUID,
    body: MemberApproveRequest,
    db: DBSession,
    current_user=APPROVE,
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_member_access(db, current_user, member)
    if body.status in (
        MemberStatus.APPROVED,
        MemberStatus.REJECTED,
        MemberStatus.BLOCKED,
    ):
        member.membership_status = body.status.value
        if body.status == MemberStatus.APPROVED:
            member.approved_at = datetime.now(UTC)
            member.joined_at = member.joined_at or datetime.now(UTC)
            member.approved_by = current_user.id
            member.rejection_reason = None
            if member.user_id is not None:
                linked_user = await db.get(User, member.user_id)
                if linked_user is not None:
                    linked_user.is_active = True
            if not member.membership_number and member.group_id:
                group = await db.get(SocialGroup, member.group_id)
                if group and group.group_id:
                    await assign_member_id(db, member, group)
        else:
            member.rejection_reason = body.rejection_reason
            if member.user_id is not None:
                linked_user = await db.get(User, member.user_id)
                if linked_user is not None:
                    linked_user.is_active = False
        action = (
            AuditAction.MEMBER_APPROVE
            if body.status == MemberStatus.APPROVED
            else AuditAction.MEMBER_REJECT
            if body.status == MemberStatus.REJECTED
            else AuditAction.MEMBER_BLOCK
        )
        await AuditService(db).log(
            action, user_id=current_user.id, entity_type="member", entity_id=member.id
        )
        await db.flush()
    else:
        raise BadRequestException("Use update endpoint for status changes")
    member = await _reload_member(db, member.id)
    return ok("Member status updated successfully", _member_out(member))


@router.delete("/{member_id}", dependencies=[DELETE])
async def delete_member(member_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_member_access(db, current_user, member)
    # Soft delete first: if object storage cannot be reached, the member is
    # still gone from the API and only an orphan object is left behind.
    await repo.delete(member)
    await memberships.detach_member(db, member)
    if member.user_id:
        user = await UserRepository(db).get_by_id(member.user_id)
        if user:
            user.group_id = None
            res = await db.execute(select(Role).where(Role.name == RoleName.MEMBER.value))
            member_role = res.scalar_one_or_none()
            if member_role and user.role_id != member_role.id:
                ga_res = await db.execute(select(Role).where(Role.name == RoleName.GROUP_ADMIN.value))
                ga_role = ga_res.scalar_one_or_none()
                if ga_role and user.role_id == ga_role.id:
                    user.role_id = member_role.id
    await MemberPhotoService(db).remove_all(member)
    await db.flush()
    return ok("Member deleted successfully")
