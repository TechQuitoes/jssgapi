from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    active_payment_link_id: str | None = None
    active_payment_link_url: str | None = None
    created_at: datetime
    updated_at: datetime


class GroupPaymentDetailOut(BaseModel):
    obligation: GroupObligationOut
    transactions: list[GroupPaymentTransactionOut] = []


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
