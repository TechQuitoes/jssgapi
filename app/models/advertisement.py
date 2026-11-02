from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import AdvertisementStatus
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.advertisement_subscription_plan import AdvertisementSubscriptionPlan
    from app.models.group import SocialGroup
    from app.models.user import User


class Advertisement(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "advertisements"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    person_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(20), default="IMAGE")
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    large_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    large_image_object_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True, default=Decimal("0.00")
    )
    subscription_months: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=1
    )
    subscription_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "advertisement_subscription_plans.id",
            name="fk_advertisements_subscription_plan_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")
    purchased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_start_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_expiry_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    placement: Mapped[str] = mapped_column(
        String(50), nullable=False, default="HOME_BANNER"
    )
    start_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AdvertisementStatus.APPROVED.value,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payment_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="PENDING"
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    advertiser_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    advertiser: Mapped[User | None] = relationship()
    group: Mapped[SocialGroup | None] = relationship(lazy="selectin")
    subscription_plan: Mapped[AdvertisementSubscriptionPlan | None] = relationship(
        lazy="selectin"
    )

    @property
    def group_name(self) -> str | None:
        return self.group.name if self.group else None

    def __repr__(self) -> str:
        return f"<Advertisement {self.title}>"
