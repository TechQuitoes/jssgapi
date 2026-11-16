"""Pydantic schemas for the Group Fee Payment feature (Flutter mobile app).

These schemas define the exact JSON shapes the Flutter app expects for:
- Fee status & calculation (GET /groups/{group_id}/fee)
- Razorpay order creation (POST /payments/create-order)
- Payment verification (POST /payments/verify)
- Group payment history (GET /groups/{group_id}/payments)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ── A. Group Fee Status ─────────────────────────────────────────────────


class GroupFeeStatusResponse(BaseModel):
    """Response for GET /api/v1/groups/{group_id}/fee.

    Tells the Flutter app how much the group owes, the calculation breakdown,
    and whether the current user is an admin who can initiate payment.
    """

    fee_id: UUID | None = None
    obligation_id: UUID | None = None
    group_id: UUID
    group_name: str
    financial_year: str
    fee_per_member: float
    active_member_count: int
    total_amount: float
    collected_amount: float = 0.0
    pending_amount: float
    due_date: str | None = None
    status: str
    is_admin: bool
    currency: str = "INR"


# ── B. Create Razorpay Order (Flutter Checkout) ─────────────────────────


class GroupFeeOrderRequest(BaseModel):
    """Request body for POST /api/v1/payments/create-order."""

    group_id: UUID
    fee_id: UUID | None = None


class GroupFeeOrderResponse(BaseModel):
    """Response for POST /api/v1/payments/create-order.

    Contains everything the Flutter Razorpay SDK needs to open Checkout.
    Note: razorpay_key_secret is NEVER returned.
    """

    payment_id: UUID
    razorpay_order_id: str
    amount: int  # Amount in paise
    currency: str = "INR"
    razorpay_key_id: str
    group_id: UUID
    group_name: str


# ── C. Payment Verification ─────────────────────────────────────────────


class GroupFeeVerifyRequest(BaseModel):
    """Request body for POST /api/v1/payments/verify (group fee flow)."""

    payment_id: UUID | None = None
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class GroupFeeVerifyResponse(BaseModel):
    """Response for POST /api/v1/payments/verify after successful signature check."""

    status: str
    message: str
    transaction_id: str
    paid_at: datetime | None = None
    receipt_url: str | None = None


# ── E. Group Payment History ────────────────────────────────────────────


class GroupPaymentHistoryItem(BaseModel):
    """Single row in the group payment history list."""

    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID
    financial_year: str
    amount: float
    currency: str = "INR"
    status: str
    transaction_id: str | None = None
    paid_at: datetime | None = None
    receipt_url: str | None = None
    payment_method: str | None = None
    created_at: datetime
