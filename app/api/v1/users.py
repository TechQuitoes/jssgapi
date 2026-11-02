from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, RoleName, resolve_role_name
from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.security import hash_password
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.user import Role, User
from app.repositories.group_repository import GroupRepository
from app.repositories.member_repository import MemberRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    AdminResetPasswordRequest,
    RoleChangeRequest,
    RoleOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services.audit_service import AuditService
from app.utils.validators import normalize_phone_number

router = APIRouter(prefix="/users", tags=["Users"])

READ = Depends(require_permission(Permission.USER_READ))
MANAGE = Depends(require_permission(Permission.USER_MANAGE))
ROLE = Depends(require_permission(Permission.ROLE_MANAGE))


async def _resolve_role(db, role_name: str | None) -> Role | None:
    if role_name is None:
        return None
    # Accept "Admin"/"admin"/"Admin\n" as the single canonical role so a
    # whitespace-polluted label can never be selected or created, and fold the
    # retired labels onto the role that replaced them.
    canonical = resolve_role_name(role_name)
    if canonical is None:
        raise BadRequestException(f"Role '{role_name}' does not exist")
    result = await db.execute(select(Role).where(Role.name == canonical.value))
    role = result.scalar_one_or_none()
    if role is None:
        raise BadRequestException(f"Role '{canonical.value}' is not configured yet")
    return role


@router.get("", dependencies=[READ])
async def list_users(
    db: DBSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = UserRepository(db)
    users = await repo.list_all(limit=page_size, offset=(page - 1) * page_size)
    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    return paginated(
        "Users fetched successfully",
        [UserOut.model_validate(u) for u in users],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[MANAGE])
async def create_user(body: UserCreate, db: DBSession):
    repo = UserRepository(db)
    if body.email and await repo.get_by_email(body.email.strip().lower()):
        raise AlreadyExistsException("This email is already registered.")
    norm_phone = (
        normalize_phone_number(body.phone_number) if body.phone_number else None
    )
    if norm_phone:
        in_use, _ = await MemberRepository(db).check_phone_in_use(norm_phone)
        if in_use:
            raise AlreadyExistsException("This phone number is already registered.")
    role = await _resolve_role(db, body.role_name)
    user = await repo.create(
        email=body.email.strip().lower() if body.email else None,
        full_name=body.full_name,
        phone_number=norm_phone,
        password_hash=hash_password(body.password),
        is_active=body.is_active,
        role_id=role.id if role else None,
    )
    await db.flush()
    return created("User created successfully", UserOut.model_validate(user))


@router.get("/roles", dependencies=[READ])
async def list_roles(db: DBSession):
    result = await db.execute(select(Role).order_by(Role.name))
    roles = [RoleOut.model_validate(r) for r in result.scalars().all()]
    return ok("Roles fetched successfully", roles)


@router.get("/{user_id}", dependencies=[READ])
async def get_user(user_id: UUID, db: DBSession):
    user = await UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    return ok("User fetched successfully", UserOut.model_validate(user))


@router.patch("/{user_id}", dependencies=[MANAGE])
async def update_user(user_id: UUID, body: UserUpdate, db: DBSession):
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    data = body.model_dump(exclude_unset=True)
    role_name = data.pop("role_name", None)
    if role_name:
        # Role changes go through PATCH /users/{id}/role, which also owns the
        # group_id bookkeeping a GROUP_ADMIN needs. Silently accepting a role here
        # would let a client demote a Group Admin into an unscoped state.
        raise BadRequestException("Use PATCH /users/{id}/role to change a user's role")
    if "phone_number" in data and data["phone_number"]:
        norm_phone = normalize_phone_number(data["phone_number"])
        existing = await repo.get_by_phone(norm_phone)
        if existing and existing.id != user.id:
            raise AlreadyExistsException("This phone number is already registered.")
        data["phone_number"] = norm_phone
    if "email" in data and data["email"]:
        clean_email = data["email"].strip().lower()
        existing = await repo.get_by_email(clean_email)
        if existing and existing.id != user.id:
            raise AlreadyExistsException("This email is already registered.")
        data["email"] = clean_email
    user = await repo.update(user, **data)
    return ok("User updated successfully", UserOut.model_validate(user))


@router.patch("/{user_id}/role", dependencies=[ROLE])
async def change_user_role(
    user_id: UUID, body: RoleChangeRequest, db: DBSession, current_user: CurrentUser
):
    """Change an account's role.

    A GROUP_ADMIN must be told which group it administers, so ``group_id`` is
    required for that role and is stored on ``User.group_id``. Clearing a
    ``group_id`` demotes the account to MEMBER, because leaving a Group Admin with
    no group would either lock it out or silently widen its scope.
    """
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))

    role = await _resolve_role(db, body.role_name)
    if role is None:
        raise BadRequestException("A role is required")

    if role.name == RoleName.GROUP_ADMIN.value:
        if body.group_id is None:
            raise BadRequestException(
                "group_id is required when assigning the GROUP_ADMIN role"
            )
        group = await GroupRepository(db).get_by_id(body.group_id)
        if group is None:
            raise NotFoundException("Group", str(body.group_id))
        user.group_id = group.id
    else:
        # A role with no group (MEMBER, SUPER_ADMIN) must not keep a stale scope.
        user.group_id = None

    if user.id == current_user.id and role.name != RoleName.SUPER_ADMIN.value:
        raise BadRequestException("You cannot remove your own administrator role")

    user.role_id = role.id
    await db.flush()
    await db.refresh(user)
    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={"role": role.name, "group_id": str(user.group_id)},
    )
    return ok("User role updated successfully", UserOut.model_validate(user))


@router.patch("/{user_id}/reset-password", dependencies=[MANAGE])
async def admin_reset_password(
    user_id: UUID,
    body: AdminResetPasswordRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """Super Admin resets any user's password.

    Requires ``USER_MANAGE`` permission (SUPER_ADMIN only).
    """
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))

    user.password_hash = hash_password(body.new_password)
    await db.flush()

    await AuditService(db).log(
        AuditAction.PASSWORD_CHANGE,
        user_id=current_user.id,
        entity_type="user",
        entity_id=user.id,
        details={"reset_by": str(current_user.id)},
    )
    return ok("Password updated successfully", UserOut.model_validate(user))


@router.delete("/{user_id}", dependencies=[MANAGE])
async def delete_user(user_id: UUID, db: DBSession):
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    await repo.delete(user)
    return ok("User deleted successfully")
