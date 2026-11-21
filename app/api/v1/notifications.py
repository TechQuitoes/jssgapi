from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, OptionalUser, require_permission
from app.models.notification import DeviceToken, Notification
from app.repositories.notification_repository import (
    DeviceTokenRepository,
    NotificationRepository,
)
from app.schemas.notification import (
    DeviceTokenOut,
    DeviceTokenRegister,
    NotificationBroadcastRequest,
    NotificationOut,
    NotificationSendRequest,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])

SEND = Depends(require_permission(Permission.NOTIFICATION_SEND))


def _now() -> datetime:
    return datetime.now(UTC)


@router.post("/device-token", response_model=DeviceTokenOut)
async def register_device(
    body: DeviceTokenRegister, current_user: CurrentUser, db: DBSession
):
    repo = DeviceTokenRepository(db)
    existing = await repo.get_by_token(body.token)
    if existing:
        existing.platform = body.platform
        existing.device_name = body.device_name
        existing.user_id = current_user.id
        existing.is_active = True
        await db.flush()
        await db.refresh(existing)
        return existing
    token = await repo.create(
        user_id=current_user.id,
        token=body.token,
        platform=body.platform,
        device_name=body.device_name,
        is_active=True,
    )
    await db.flush()
    return token


@router.get("/device-tokens")
async def list_my_devices(current_user: CurrentUser, db: DBSession):
    tokens = await DeviceTokenRepository(db).list_for_user(current_user.id)
    return ok(
        "Device tokens fetched successfully",
        [DeviceTokenOut.model_validate(t) for t in tokens],
    )


@router.delete("/device-token/{token_id}")
async def remove_device(token_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = DeviceTokenRepository(db)
    token = await repo.get_by_id(token_id)
    if token is None or token.user_id != current_user.id:
        raise NotFoundException("Device token", str(token_id))
    token.is_active = False
    await db.flush()
    return ok("Device token removed successfully")


@router.get("")
async def list_notifications(
    db: DBSession,
    current_user: OptionalUser = None,
    category: str | None = Query(default=None, description="Filter by category: contribution, group_payment, ad_payment, payment, or all"),
    unread_only: bool = Query(default=False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = NotificationRepository(db)
    user_id = current_user.id if current_user else None
    if unread_only and user_id is not None:
        items = await repo.list_for_user(
            user_id, limit=page_size, offset=(page - 1) * page_size, category=category
        )
        items = [n for n in items if not n.is_read]
        unseen = await repo.count_unread(user_id, category=category)
        return ok(
            "Unread notifications fetched successfully",
            {
                "unread_count": unseen,
                "items": [NotificationOut.model_validate(n) for n in items],
            },
        )
    items = await repo.list_for_user(
        user_id, limit=page_size, offset=(page - 1) * page_size, category=category
    )
    total = await repo.count_for_user(user_id, category=category)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return paginated(
        "Notifications fetched successfully",
        [NotificationOut.model_validate(n) for n in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    )


@router.post("/send", dependencies=[SEND])
async def send_notification(
    body: NotificationSendRequest, db: DBSession, current_user: CurrentUser
):
    repo = NotificationRepository(db)
    notification = await repo.create(
        recipient_id=body.recipient_id,
        title=body.title,
        body=body.body,
        data=body.data,
        notification_type=body.notification_type,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        sent_at=_now(),
    )
    await db.flush()
    return created(
        "Notification sent successfully", NotificationOut.model_validate(notification)
    )


@router.post("/broadcast", dependencies=[SEND])
async def broadcast_notification(
    body: NotificationBroadcastRequest, db: DBSession, current_user: CurrentUser
):
    repo = NotificationRepository(db)
    notification = await repo.create(
        recipient_id=None,
        title=body.title,
        body=body.body,
        notification_type=body.notification_type,
        sent_at=_now(),
    )
    await db.flush()
    return ok(
        "Notification broadcast to recipients",
        NotificationOut.model_validate(notification),
    )


@router.get("/{notification_id}")
async def get_notification(
    notification_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = NotificationRepository(db)
    notification = await repo.get_by_id(notification_id)
    if (
        notification is None
        or (notification.recipient_id is not None and notification.recipient_id != current_user.id)
    ):
        raise NotFoundException("Notification", str(notification_id))
    return ok(
        "Notification fetched successfully",
        NotificationOut.model_validate(notification),
    )


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = NotificationRepository(db)
    notification = await repo.get_by_id(notification_id)
    if (
        notification is None
        or (notification.recipient_id is not None and notification.recipient_id != current_user.id)
    ):
        raise NotFoundException("Notification", str(notification_id))
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = _now()
        await db.flush()
        await db.refresh(notification)
    return ok(
        "Notification marked as read", NotificationOut.model_validate(notification)
    )


@router.post("/read-all")
async def mark_all_read(current_user: CurrentUser, db: DBSession):
    from sqlalchemy import or_

    result = await db.execute(
        select(Notification).where(
            or_(
                Notification.recipient_id == current_user.id,
                Notification.recipient_id.is_(None),
            ),
            Notification.is_read.is_(False),
        )
    )
    updated = 0
    now = _now()
    for notification in result.scalars().all():
        notification.is_read = True
        notification.read_at = now
        updated += 1
    await db.flush()
    return ok(f"Marked {updated} notifications as read")
