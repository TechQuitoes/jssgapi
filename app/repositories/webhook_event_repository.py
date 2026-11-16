from __future__ import annotations

from sqlalchemy import select

from app.models.webhook_event import WebhookEvent
from app.repositories.base_repository import BaseRepository


class WebhookEventRepository(BaseRepository[WebhookEvent]):
    model = WebhookEvent

    async def get_by_event_id(self, event_id: str) -> WebhookEvent | None:
        result = await self.session.execute(
            select(WebhookEvent).where(WebhookEvent.event_id == event_id)
        )
        return result.scalar_one_or_none()
