from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.constants import (
    GroupObligationStatus,
    MemberStatus,
    PaymentStatus,
    PaymentWorkflow,
    RoleName,
)
from app.core.exceptions import BadRequestException
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.user import Role, User
from app.services.payment_service import PaymentService


@pytest.fixture
def mock_razorpay():
    with patch("app.services.payment_service.create_razorpay_order") as mock_order, \
         patch("app.services.payment_service.fetch_razorpay_payment") as mock_fetch_pay, \
         patch("app.services.payment_service.verify_razorpay_payment_signature") as mock_sig:

        mock_order.return_value = {
            "id": "order_contrib_98765",
            "amount": 50100,
            "currency": "INR",
            "status": "created",
        }
        mock_fetch_pay.return_value = {
            "id": "pay_contrib_45678",
            "order_id": "order_contrib_98765",
            "status": "captured",
            "amount": 50100,
            "currency": "INR",
            "method": "upi",
        }
        mock_sig.return_value = True

        yield {
            "order": mock_order,
            "fetch_pay": mock_fetch_pay,
            "sig": mock_sig,
        }


@pytest.mark.asyncio
async def test_create_contribution_order(db_session, mock_razorpay):
    """Test creating a contribution order with custom amount and purpose."""
    role = await Role.get_or_create(db_session, RoleName.MEMBER)
    user = User(
        full_name="Mahavir Shah",
        phone_number="9876543210",
        password_hash="hash",
        role_id=role.id,
    )
    db_session.add(user)

    group = SocialGroup(name="Indore Mandal", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    service = PaymentService(db_session)
    result = await service.create_contribution_order(
        amount=Decimal("501.00"),
        user=user,
        group_id=group.id,
        purpose="Mandir Jirnoddhar Sahayog",
        notes="Anumodna from family",
    )

    assert result["razorpay_order_id"] == "order_contrib_98765"
    assert result["amount"] == 50100
    assert result["amount_in_rupees"] == Decimal("501.00")
    assert result["currency"] == "INR"
    assert result["group_id"] == group.id
    assert result["purpose"] == "Mandir Jirnoddhar Sahayog"


@pytest.mark.asyncio
async def test_multiple_contributions_allowed_when_group_fee_paid(db_session, mock_razorpay):
    """Voluntary contributions must succeed multiple times even if group fee is already PAID."""
    role = await Role.get_or_create(db_session, RoleName.MEMBER)
    user = User(
        full_name="Suresh Jain",
        phone_number="9876543211",
        password_hash="hash",
        role_id=role.id,
    )
    db_session.add(user)

    group = SocialGroup(name="Bhopal Mandal", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    service = PaymentService(db_session)

    # 1. Simulate group fee obligation as already PAID
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")
    obligation.status = GroupObligationStatus.PAID.value
    obligation.pending_amount = Decimal("0.00")
    obligation.paid_amount = Decimal("1000.00")
    await db_session.flush()

    # 2. First contribution (e.g. ₹500)
    res1 = await service.create_contribution_order(
        amount=Decimal("500.00"),
        user=user,
        group_id=group.id,
        purpose="Donation 1",
    )
    assert res1["razorpay_order_id"] == "order_contrib_98765"

    # 3. Second contribution immediately for same group (e.g. ₹1500)
    mock_razorpay["order"].return_value = {
        "id": "order_contrib_98766",
        "amount": 150000,
        "currency": "INR",
        "status": "created",
    }
    res2 = await service.create_contribution_order(
        amount=Decimal("1500.00"),
        user=user,
        group_id=group.id,
        purpose="Donation 2",
    )
    assert res2["razorpay_order_id"] == "order_contrib_98766"


@pytest.mark.asyncio
async def test_create_contribution_invalid_amount(db_session, mock_razorpay):
    """Zero or negative contribution amount must be rejected."""
    role = await Role.get_or_create(db_session, RoleName.MEMBER)
    user = User(
        full_name="Test User",
        phone_number="9876543212",
        password_hash="hash",
        role_id=role.id,
    )
    db_session.add(user)
    await db_session.flush()

    service = PaymentService(db_session)
    with pytest.raises(BadRequestException, match="must be greater than 0"):
        await service.create_contribution_order(
            amount=Decimal("0.00"),
            user=user,
        )


@pytest.mark.asyncio
async def test_verify_contribution_payment(db_session, mock_razorpay):
    """Signature verification and status update to SUCCESS."""
    role = await Role.get_or_create(db_session, RoleName.MEMBER)
    user = User(
        full_name="Pankaj Jain",
        phone_number="9876543213",
        password_hash="hash",
        role_id=role.id,
    )
    db_session.add(user)
    await db_session.flush()

    service = PaymentService(db_session)
    order_res = await service.create_contribution_order(
        amount=Decimal("501.00"),
        user=user,
        purpose="Seva Sahayog",
    )

    verify_res = await service.verify_contribution_payment(
        razorpay_order_id=order_res["razorpay_order_id"],
        razorpay_payment_id="pay_contrib_45678",
        razorpay_signature="dummy_signature",
    )

    assert verify_res["status"] == "SUCCESS"
    assert verify_res["transaction_id"] == "pay_contrib_45678"
    assert verify_res["amount"] == Decimal("501.00")
    assert verify_res["purpose"] == "Seva Sahayog"
    assert verify_res["donor_name"] == "Pankaj Jain"

    # Idempotent re-verify check
    reverify_res = await service.verify_contribution_payment(
        razorpay_order_id=order_res["razorpay_order_id"],
        razorpay_payment_id="pay_contrib_45678",
        razorpay_signature="dummy_signature",
    )
    assert reverify_res["status"] == "SUCCESS"
    assert "already verified" in reverify_res["message"]
