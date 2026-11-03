from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, Time
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import EventType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.event_group import EventGroup
    from app.models.group import SocialGroup
    from app.models.location import Location
    from app.models.user import User


class Event(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "events"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    title_hi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The date and time are local wall-clock values, exactly as the host wrote
    # them down ("25/12/2026", "18:30"). ``starts_at`` is the instant they
    # refer to and is derived from them, so there is a single moment to sort
    # and compare on. The server defaults only exist so a column added to a
    # populated table can be backfilled; the API always supplies both values.
    event_date: Mapped[date] = mapped_column(
        Date, nullable=False, server_default="CURRENT_DATE", index=True
    )
    event_time: Mapped[time] = mapped_column(
        Time, nullable=False, server_default="CURRENT_TIME"
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    venue_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    venue_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The storage key of the cover object, so a replacement can delete the
    # object it replaces.
    cover_image_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    # The kind of programme the host chose. ``event_type_other`` carries the
    # label when the type is OTHER.
    event_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=EventType.OTHER.value,
        server_default="'OTHER'",
        index=True,
    )
    event_type_other: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Who hosts the event. Defaults to the account that created it, but the
    # host can name a different person or contact number for a given event.
    host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    host_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host_contact_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PUBLISHED", index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_attendees: Mapped[int | None] = mapped_column(nullable=True)
    registration_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    group: Mapped[SocialGroup | None] = relationship()
    event_groups: Mapped[list[EventGroup]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="EventGroup.created_at",
    )
    location: Mapped[Location | None] = relationship()
    host: Mapped[User | None] = relationship()

    @property
    def group_ids(self) -> list[uuid.UUID]:
        """Every selected group, with the legacy primary group first."""
        ordered: list[uuid.UUID] = []
        if self.group_id is not None:
            ordered.append(self.group_id)
        for link in self.event_groups:
            if link.group_id not in ordered:
                ordered.append(link.group_id)
        return ordered

    def __repr__(self) -> str:
        return f"<Event {self.title}>"
