from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select

from app.models.notification import DeviceToken, Notification
from app.repositories.base_repository import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    def _apply_filters(self, user_id: UUID | None, category: str | None = None):
        conds = []
        if user_id is not None:
            conds.append(
                or_(
                    Notification.recipient_id == user_id,
                    Notification.recipient_id.is_(None),
                )
            )
        else:
            conds.append(Notification.recipient_id.is_(None))

        if category and category.lower() not in ("all", "all received"):
            cat_lower = category.lower()
            cat_upper = category.upper()
            conds.append(
                or_(
                    Notification.notification_type == cat_upper,
                    Notification.notification_type == cat_lower,
                    Notification.data.like(f'%"category": "{cat_lower}"%'),
                )
            )
        return conds

    async def list_for_user(
        self, user_id: UUID | None, limit: int, offset: int, category: str | None = None
    ) -> list[Notification]:
        filters = self._apply_filters(user_id, category)
        result = await self.session.execute(
            select(Notification)
            .where(*filters)
            .order_by(Notification.sent_at.desc().nullslast(), Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: UUID | None, category: str | None = None) -> int:
        filters = self._apply_filters(user_id, category)
        result = await self.session.execute(
            select(func.count(Notification.id)).where(*filters)
        )
        return result.scalar() or 0

    async def count_unread(self, user_id: UUID | None, category: str | None = None) -> int:
        if user_id is None:
            return 0
        filters = self._apply_filters(user_id, category)
        result = await self.session.execute(
            select(func.count(Notification.id)).where(
                *filters,
                Notification.is_read.is_(False),
            )
        )
        return result.scalar() or 0


class DeviceTokenRepository(BaseRepository[DeviceToken]):
    model = DeviceToken

    async def get_by_token(self, token: str) -> DeviceToken | None:
        result = await self.session.execute(
            select(DeviceToken).where(DeviceToken.token == token)
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id) -> list[DeviceToken]:
        result = await self.session.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id, DeviceToken.is_active.is_(True)
            )
        )
        return list(result.scalars().all())
