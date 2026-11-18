from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import GroupObligationStatus
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.fee import Fee
    from app.models.group import SocialGroup
    from app.models.payment import Payment, PaymentTransaction


class GroupPaymentObligation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Group-level payment obligation and ledger snapshot.

    Tracks expected vs collected amounts for a group in a given billing period.
    Financial data is protected from cascading group deletion: ``group_id``
    uses ON DELETE RESTRICT to preserve accounting integrity.
    """

    __tablename__ = "group_payment_obligations"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "billing_period", name="uq_group_obligation_period"
        ),
        CheckConstraint(
            "member_count_snapshot >= 0", name="ck_obligation_member_count"
        ),
        CheckConstraint(
            "rate_per_member >= 0", name="ck_obligation_rate_per_member"
        ),
        CheckConstraint(
            "expected_amount >= 0", name="ck_obligation_expected_amount"
        ),
        CheckConstraint(
            "collected_amount >= 0", name="ck_obligation_collected_amount"
        ),
        CheckConstraint(
            "pending_amount >= 0", name="ck_obligation_pending_amount"
        ),
        Index("ix_group_payment_obligations_group_id", "group_id"),
        Index("ix_group_payment_obligations_status", "status"),
        Index("ix_group_payment_obligations_period", "billing_period"),
    )

    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="RESTRICT"),
        nullable=True,
    )
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    billing_period: Mapped[str] = mapped_column(String(20), nullable=False)
    fee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fees.id", ondelete="SET NULL"),
        nullable=True,
    )

    member_count_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    rate_per_member: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=100.00
    )
    expected_amount: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0.00
    )
    collected_amount: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0.00
    )
    pending_amount: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0.00
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=GroupObligationStatus.UNPAID.value,
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    active_payment_link_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    active_payment_link_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    group: Mapped[SocialGroup | None] = relationship()
    fee: Mapped[Fee | None] = relationship()
    payments: Mapped[list[Payment]] = relationship(
        back_populates="obligation", lazy="selectin"
    )
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        back_populates="obligation", lazy="selectin"
    )

    def __repr__(self) -> str:
        return (
            f"<GroupPaymentObligation {self.group_name} {self.billing_period} "
            f"Expected:₹{self.expected_amount} Collected:₹{self.collected_amount} {self.status}>"
        )
