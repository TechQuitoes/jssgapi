from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PaymentSummaryOut(BaseModel):
    total_expected: float = 0.0
    total_collected: float = 0.0
    total_outstanding: float = 0.0
    total_transactions: int = 0


class GroupPaymentTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    transaction_type: str
    status: str
    amount: float | None = None
    payment_method: str | None = None
    razorpay_order_id: str | None = None
    razorpay_payment_id: str | None = None
    razorpay_payment_link_id: str | None = None
    provider_transaction_id: str | None = None
    paid_at: datetime | None = None
    created_at: datetime


class GroupObligationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    group_id: UUID | None
    group_name: str
    billing_period: str
    member_count: int = Field(alias="member_count_snapshot")
    rate_per_member: float
    expected_amount: float
    collected_amount: float
    pending_amount: float
    collection_percentage: float = 0.0
    status: str
    due_date: str | None = None
    active_payment_link_id: str | None = None
    active_payment_link_url: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("due_date", mode="before")
    def _parse_due_date(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, (date, datetime)):
            return v.strftime("%Y-%m-%d")
        return str(v)


class GroupPaymentDetailOut(BaseModel):
    obligation: GroupObligationOut
    transactions: list[GroupPaymentTransactionOut] = []


class GroupCheckoutOrderCreateRequest(BaseModel):
    amount: float | None = None
    billing_period: str | None = None


class PaymentSettingsModel(BaseModel):
    rate_per_member: float = 100.0
    billing_period: str = "2026-2027"
    due_date: str = "2027-03-31"
    late_fee: float = 0.0
    allow_partial_payments: bool = True
    min_partial_amount: float = 100.0
    payment_gateway: str = "Razorpay"
    razorpay_key_id: str = "rzp_test_TzeUn0Iazq0yuq"
    allowed_payment_methods: list[str] = ["UPI", "Net Banking", "Debit/Credit Card"]
    apply_to_existing_obligations: bool = False


class GroupPaymentOrderCreateResponse(BaseModel):
    order_id: str
    razorpay_order_id: str
    razorpay_key_id: str
    amount: float
    currency: str = "INR"
    group_id: UUID
    group_name: str
    obligation_id: UUID
    status: str


class GroupPaymentVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class GroupPaymentLinkResponse(BaseModel):
    payment_link_id: str
    short_url: str
    amount: float
    currency: str = "INR"
    group_id: UUID
    group_name: str
    obligation_id: UUID
    status: str
