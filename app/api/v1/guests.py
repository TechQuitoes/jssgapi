from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.constants import AuditAction, RoleName, resolve_role_name
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.responses import ok, paginated
from app.dependencies import CurrentUser, DBSession
from app.models.user import Role, User
from app.schemas.guest import GuestApproveRequest, GuestOut
from app.services.audit_service import AuditService
from app.services.notification_service import send_push_notification_to_user

router = APIRouter(prefix="/guests", tags=["Guests"])


def _assert_super_admin(user: User) -> None:
    role = resolve_role_name(user.role.name) if user.role else None
    if role is not RoleName.SUPER_ADMIN:
        raise ForbiddenException("Only Super Admin can manage Guest users.")


@router.get("")
async def list_guests(
    db: DBSession,
    current_user: CurrentUser,
    status: str | None = Query(
        default=None, description="Filter by status: PENDING or APPROVED"
    ),
    query: str | None = Query(
        default=None, max_length=100, description="Search by name or phone"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List all registered guests. Restricted to Super Admin."""
    _assert_super_admin(current_user)

    stmt = (
        select(User)
        .join(Role, User.role_id == Role.id)
        .where(
            Role.name == RoleName.GUEST.value,
            User.is_deleted.is_(False),
        )
    )

    if status:
        st = status.strip().upper()
        if st == "PENDING":
            stmt = stmt.where(User.is_active.is_(False))
        elif st == "APPROVED":
            stmt = stmt.where(User.is_active.is_(True))

    if query and query.strip():
        q = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(q),
                User.phone_number.ilike(q),
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    result = await db.execute(
        stmt.order_by(User.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    users = result.scalars().all()

    items = [
        GuestOut(
            id=u.id,
            full_name=u.full_name,
            phone_number=u.phone_number,
            email=u.email,
            is_active=u.is_active,
            status="APPROVED" if u.is_active else "PENDING",
            role="GUEST",
            tag="GUEST",
            created_at=u.created_at,
            updated_at=u.updated_at,
            last_login_at=u.last_login_at,
        )
        for u in users
    ]

    return paginated(
        "Guests fetched successfully",
        [item.model_dump(mode="json") for item in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("/{guest_id}/approve")
async def approve_guest(
    guest_id: UUID,
    body: GuestApproveRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """Approve or reject a Guest registration request. Super Admin only."""
    _assert_super_admin(current_user)

    stmt = (
        select(User)
        .options(selectinload(User.role))
        .where(User.id == guest_id)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.is_deleted:
        raise NotFoundException("Guest", str(guest_id))

    user_role = resolve_role_name(user.role.name) if user.role else None
    if user_role is not RoleName.GUEST:
        raise ForbiddenException("This user is not a Guest account.")

    is_approve = body.status.strip().upper() == "APPROVED"
    user.is_active = is_approve
    await db.flush()
    await db.refresh(user)

    action = AuditAction.MEMBER_APPROVE if is_approve else AuditAction.MEMBER_REJECT
    await AuditService(db).log(
        action,
        user_id=current_user.id,
        entity_type="guest",
        entity_id=user.id,
        details={
            "status": "APPROVED" if is_approve else "REJECTED",
            "reason": body.rejection_reason,
        },
    )

    if is_approve:
        try:
            await send_push_notification_to_user(
                db,
                user_id=user.id,
                title="Guest Account Approved",
                body="Your Guest account has been approved by Super Admin. You can now log in.",
                data={"type": "GUEST_APPROVED"},
            )
        except Exception:
            pass

    status_str = "approved" if is_approve else "rejected"
    return ok(
        f"Guest {status_str} successfully",
        GuestOut(
            id=user.id,
            full_name=user.full_name,
            phone_number=user.phone_number,
            email=user.email,
            is_active=user.is_active,
            status="APPROVED" if user.is_active else "PENDING",
            role="GUEST",
            tag="GUEST",
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login_at=user.last_login_at,
        ).model_dump(mode="json"),
    )


@router.delete("/{guest_id}")
async def delete_guest(
    guest_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Delete a Guest account. Super Admin only."""
    _assert_super_admin(current_user)

    user = await db.get(User, guest_id)
    if user is None or user.is_deleted:
        raise NotFoundException("Guest", str(guest_id))

    user_role = resolve_role_name(user.role.name) if user.role else None
    if user_role is not RoleName.GUEST:
        raise ForbiddenException("This user is not a Guest account.")

    user.soft_delete()
    await db.flush()

    return ok("Guest deleted successfully")
