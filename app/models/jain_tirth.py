from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import EventStatus
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.group import SocialGroup


class JainTirth(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "jain_tirths"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_person_name_1: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    contact_person_phone_1: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    contact_person_name_2: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    contact_person_phone_2: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_thumb_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EventStatus.PUBLISHED.value,
        server_default=f"'{EventStatus.PUBLISHED.value}'",
        index=True,
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    group: Mapped[SocialGroup | None] = relationship()

    def __repr__(self) -> str:
        return f"<JainTirth {self.title}>"
