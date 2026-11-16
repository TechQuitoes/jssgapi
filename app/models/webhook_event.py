from __future__ import annotations

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class WebhookEvent(Base, UUIDPrimaryKeyMixin):
    """Log of incoming webhook events for strict idempotency and auditability."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        Index("ix_webhook_events_event_id", "event_id", unique=True),
        Index("ix_webhook_events_payment_id", "payment_id"),
        Index("ix_webhook_events_order_id", "order_id"),
    )

    event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_link_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PROCESSED")
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<WebhookEvent {self.event_id} {self.event_type} {self.status}>"
