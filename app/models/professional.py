from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.member import Member


class ProfessionalInformation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "professional_information"

    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    occupation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occupation_hi: Mapped[str | None] = mapped_column(String(400), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name_hi: Mapped[str | None] = mapped_column(String(400), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    designation_hi: Mapped[str | None] = mapped_column(String(400), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    business_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_visible_in_directory: Mapped[bool] = mapped_column(default=True, nullable=False)

    member: Mapped[Member] = relationship(back_populates="professional_info")

    def __repr__(self) -> str:
        return f"<ProfessionalInformation {self.occupation}>"
