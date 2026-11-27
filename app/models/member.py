from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import MemberStatus
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.family import FamilyMember
    from app.models.group import SocialGroup
    from app.models.location import Location
    from app.models.member_group import MemberGroupMembership
    from app.models.professional import ProfessionalInformation
    from app.models.user import User


class Member(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_members_user_id"),)

    # A community member record is NOT the same thing as a login account. Imported /
    # Google Sheet members have no User, and an Admin must be able to manage them, so
    # the link is optional. `SET NULL` (not CASCADE) keeps the member profile when a
    # User is removed - deleting a login must never delete community data. Postgres
    # allows many NULLs under a UNIQUE constraint, so one-member-per-user still holds.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    first_name_hi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    middle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    middle_name_hi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name_hi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(10), nullable=True)
    profile_photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_photo_thumb_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Photo links copied out of the Google Sheet. The sheet still holds Drive
    # links, and the app can only render bytes over plain HTTP, so a Drive link
    # is copied to R2 and the original is kept here for reference. Once
    # ``*_r2_object_key`` is set the photo is considered migrated and the
    # migration skips it.
    profile_photo_drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_photo_r2_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    profile_photo_r2_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    spouse_photo_drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    spouse_photo_r2_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    spouse_photo_r2_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_line_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    occupation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    occupation_summary_hi: Mapped[str | None] = mapped_column(Text, nullable=True)

    membership_number: Mapped[str | None] = mapped_column(
        String(50), unique=True, nullable=True, index=True
    )
    membership_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemberStatus.PENDING.value, index=True
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_profile_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_post_holder: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False, index=True
    )
    post_holder_title: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    user: Mapped[User | None] = relationship(back_populates="member", lazy="selectin")
    group: Mapped[SocialGroup | None] = relationship(back_populates="members")
    group_memberships: Mapped[list[MemberGroupMembership]] = relationship(
        back_populates="member",
        lazy="selectin",
        order_by="MemberGroupMembership.priority",
        cascade="all, delete-orphan",
    )
    location: Mapped[Location | None] = relationship(back_populates="members")
    family_members: Mapped[list[FamilyMember]] = relationship(
        back_populates="member", lazy="selectin"
    )
    professional_info: Mapped[list[ProfessionalInformation]] = relationship(
        back_populates="member", lazy="selectin", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Member {self.first_name} {self.last_name}>"
