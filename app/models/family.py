from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.member import Member


class FamilyMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "family_members"

    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_hi: Mapped[str | None] = mapped_column(String(400), nullable=True)
    relationship_type: Mapped[str] = mapped_column(
        "relationship", String(100), nullable=False
    )
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    occupation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occupation_hi: Mapped[str | None] = mapped_column(String(400), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    member: Mapped[Member] = relationship(back_populates="family_members")

    def __repr__(self) -> str:
        return f"<FamilyMember {self.name} ({self.relationship_type})>"
