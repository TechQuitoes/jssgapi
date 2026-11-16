from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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
