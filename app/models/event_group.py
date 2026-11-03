from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.group import SocialGroup


class EventGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A social group selected for an event."""

    __tablename__ = "event_groups"
    __table_args__ = (
        UniqueConstraint("event_id", "group_id", name="uq_event_groups_event_group"),
        Index("ix_event_groups_group_id", "group_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="CASCADE"),
        nullable=False,
    )

    event: Mapped[Event] = relationship(back_populates="event_groups")
    group: Mapped[SocialGroup] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        return f"<EventGroup event={self.event_id} group={self.group_id}>"
