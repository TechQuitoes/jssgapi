from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import PaymentStatus


class PaymentCreate(BaseModel):
    member_id: UUID | None = None
    fee_id: UUID | None = None
    amount: Decimal
    currency: str = "INR"


class PaymentVerifyRequest(BaseModel):
    """Payload sent by the Flutter app after Razorpay Checkout completes."""

    payment_id: UUID | None = None
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class PaymentTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    transaction_type: str
    status: str
    provider_transaction_id: str | None = None
    amount: Decimal | None = None
    created_at: datetime


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID | None = None
    fee_id: UUID | None = None
    order_id: str
    amount: Decimal
    currency: str
    status: str
    provider: str
    provider_reference: str | None = None
    paid_at: datetime | None = None
    expires_at: datetime | None = None
    transactions: list[PaymentTransactionOut] = []
    created_at: datetime
    updated_at: datetime


class PaymentCreateResponse(BaseModel):
    """Returned to the Flutter app so it can launch Razorpay Checkout."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: str
    razorpay_order_id: str
    razorpay_key_id: str
    amount: Decimal
    currency: str
    status: str
    provider: str
    created_at: datetime


class ContributionOrderRequest(BaseModel):
    """Request payload to initiate a voluntary contribution / donation."""

    amount: Decimal = Field(..., gt=0, description="Contribution amount in INR (rupees)")
    group_id: UUID | None = Field(default=None, description="Optional group ID to associate contribution with")
    event_id: UUID | None = Field(default=None, description="Optional event ID if contributing to a specific event")
    purpose: str | None = Field(default=None, max_length=255, description="Purpose or title of contribution")
    notes: str | None = Field(default=None, max_length=500, description="Optional donor notes or message")


class ContributionOrderResponse(BaseModel):
    """Response returned so Flutter/web app can open Razorpay checkout."""

    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID
    order_id: str
    razorpay_order_id: str
    razorpay_key_id: str
    amount: int = Field(..., description="Amount in paise for Razorpay Checkout")
    amount_in_rupees: Decimal = Field(..., description="Amount in rupees")
    currency: str = "INR"
    group_id: UUID | None = None
    purpose: str | None = None


class ContributionVerifyRequest(BaseModel):
    """Payload sent by client after Razorpay Checkout completes."""

    payment_id: UUID | None = None
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class ContributionVerifyResponse(BaseModel):
    """Response returned after successful contribution verification."""

    model_config = ConfigDict(from_attributes=True)

    status: str
    message: str
    transaction_id: str
    amount: Decimal
    currency: str = "INR"
    paid_at: datetime | None = None
    purpose: str | None = None
    donor_name: str | None = None
    receipt_url: str | None = None


class ContributionHistoryItem(BaseModel):
    """Single contribution record in history list."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: str
    transaction_id: str | None = None
    amount: Decimal
    currency: str
    status: str
    group_id: UUID | None = None
    group_name: str | None = None
    member_id: UUID | None = None
    purpose: str | None = None
    notes: str | None = None
    paid_at: datetime | None = None
    created_at: datetime

