from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.core.constants import (
    GroupObligationStatus,
    MemberStatus,
    PaymentStatus,
    PaymentWorkflow,
)
from app.core.exceptions import BadRequestException
from app.models.group import SocialGroup
from app.models.group_payment_obligation import GroupPaymentObligation
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.payment import Payment, PaymentTransaction
from app.services.payment_service import PaymentService


@pytest.fixture
def mock_razorpay():
    with patch("app.services.payment_service.create_razorpay_order") as mock_order, \
         patch("app.services.payment_service.create_razorpay_payment_link") as mock_link, \
         patch("app.services.payment_service.fetch_razorpay_payment") as mock_fetch_pay, \
         patch("app.services.payment_service.fetch_razorpay_payment_link") as mock_fetch_link, \
         patch("app.services.payment_service.cancel_razorpay_payment_link") as mock_cancel_link, \
         patch("app.services.payment_service.verify_razorpay_payment_signature") as mock_sig, \
         patch("app.services.payment_service.verify_razorpay_webhook_signature") as mock_wh_sig:

        mock_order.return_value = {
            "id": "order_test_12345",
            "amount": 2650000,
            "currency": "INR",
            "status": "created",
        }
        mock_link.return_value = {
            "id": "plink_test_99999",
            "short_url": "https://rzp.io/i/test999",
            "amount": 2650000,
            "currency": "INR",
            "status": "created",
        }
        mock_fetch_pay.return_value = {
            "id": "pay_test_77777",
            "order_id": "order_test_12345",
            "status": "captured",
            "amount": 2650000,
            "currency": "INR",
            "method": "upi",
        }
        mock_fetch_link.return_value = {
            "id": "plink_test_99999",
            "status": "created",
            "amount": 2650000,
        }
        mock_cancel_link.return_value = {"id": "plink_test_99999", "status": "cancelled"}
        mock_sig.return_value = True
        mock_wh_sig.return_value = True

        yield {
            "order": mock_order,
            "link": mock_link,
            "fetch_pay": mock_fetch_pay,
            "fetch_link": mock_fetch_link,
            "cancel_link": mock_cancel_link,
            "sig": mock_sig,
            "wh_sig": mock_wh_sig,
        }


@pytest.mark.asyncio
async def test_obligation_calculation_265_members(db_session, mock_razorpay):
    """Test Case 2: 265 eligible members = ₹26,500 expected."""
    group = SocialGroup(name="Indore Jain Mitra Mandal", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # Add 265 approved members
    for i in range(265):
        m = Member(
            first_name="Member",
            last_name=f"{i}",
            membership_status=MemberStatus.APPROVED.value,
            group_id=group.id,
        )
        db_session.add(m)
    # Add 5 rejected / blocked members (must NOT be counted)
    db_session.add(Member(first_name="Rej", last_name="1", membership_status=MemberStatus.REJECTED.value, group_id=group.id))
    db_session.add(Member(first_name="Blk", last_name="2", membership_status=MemberStatus.BLOCKED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    assert obligation.member_count_snapshot == 265
    assert obligation.rate_per_member == Decimal("100.00")
    assert obligation.expected_amount == Decimal("26500.00")
    assert obligation.collected_amount == Decimal("0.00")
    assert obligation.pending_amount == Decimal("26500.00")
    assert obligation.status == GroupObligationStatus.UNPAID.value


@pytest.mark.asyncio
async def test_zero_eligible_members_not_applicable(db_session, mock_razorpay):
    """Test Case 3 & Requirement 5: Zero eligible members should be NOT_APPLICABLE, not PAID."""
    group = SocialGroup(name="Empty Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    assert obligation.member_count_snapshot == 0
    assert obligation.expected_amount == Decimal("0.00")
    assert obligation.status == GroupObligationStatus.NOT_APPLICABLE.value


@pytest.mark.asyncio
async def test_distinct_member_counts_multi_group(db_session, mock_razorpay):
    """Test Case 6: Distinct member counting across primary and multi-group memberships."""
    group = SocialGroup(name="MultiGroup Branch", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # Member with primary group_id AND a MemberGroupMembership record for the same group
    m = Member(first_name="Duplicate", last_name="Check", membership_status=MemberStatus.APPROVED.value, group_id=group.id)
    db_session.add(m)
    await db_session.flush()

    membership = MemberGroupMembership(member_id=m.id, group_id=group.id, priority=1)
    db_session.add(membership)
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    # Member must only be counted ONCE
    assert obligation.member_count_snapshot == 1
    assert obligation.expected_amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_super_admin_checkout_workflow(db_session, mock_razorpay):
    """Test Case 5: Super Admin Pay Now creates order, verifies, and updates ledger."""
    group = SocialGroup(name="Bhopal Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # 10 members = ₹1,000 expected
    for i in range(10):
        db_session.add(Member(first_name="M", last_name=str(i), membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    order_res = await service.create_group_checkout_order(group.id, "2026-2027")
    assert order_res.amount == 1000.0
    assert order_res.order_id == "order_test_12345"

    # Mock payment verification of ₹1,000
    mock_razorpay["fetch_pay"].return_value = {
        "id": "pay_checkout_001",
        "order_id": order_res.order_id,
        "status": "captured",
        "amount": 100000,
        "currency": "INR",
        "method": "netbanking",
    }

    detail = await service.verify_group_checkout_payment(
        group.id,
        razorpay_order_id=order_res.order_id,
        razorpay_payment_id="pay_checkout_001",
        razorpay_signature="dummy_valid_sig",
    )

    assert detail.obligation.collected_amount == 1000.0
    assert detail.obligation.pending_amount == 0.0
    assert detail.obligation.status == GroupObligationStatus.PAID.value


@pytest.mark.asyncio
async def test_group_admin_payment_link_workflow(db_session, mock_razorpay):
    """Test Case 6: Group Admin payment link generates short URL and updates same ledger via webhook."""
    group = SocialGroup(name="Ujjain Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # 5 members = ₹500 expected
    for i in range(5):
        db_session.add(Member(first_name="M", last_name=str(i), membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    link_res = await service.create_group_payment_link(group.id, "2026-2027")
    assert link_res.short_url == "https://rzp.io/i/test999"
    assert link_res.amount == 500.0

    # Simulate webhook payment_link.paid
    webhook_payload = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_res.payment_link_id,
                    "notes": {"obligation_id": str(link_res.obligation_id)},
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_plink_captured_111",
                }
            },
        },
    }

    mock_razorpay["fetch_pay"].return_value = {
        "id": "pay_plink_captured_111",
        "status": "captured",
        "amount": 50000,
        "currency": "INR",
        "method": "upi",
    }

    body = json.dumps(webhook_payload).encode()
    res = await service.handle_webhook(body=body, signature="valid_wh_sig")
    assert res["status"] == "processed"

    # Check obligation in database
    detail = await service.get_group_obligation_detail(group.id, "2026-2027")
    assert detail.obligation.collected_amount == 500.0
    assert detail.obligation.pending_amount == 0.0
    assert detail.obligation.status == GroupObligationStatus.PAID.value


@pytest.mark.asyncio
async def test_dual_workflow_accumulation(db_session, mock_razorpay):
    """Test Case 7: Partial payment via Super Admin Checkout + partial payment via Link accumulate correctly."""
    group = SocialGroup(name="Dual Workflow Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # 10 members = ₹1,000 expected
    for i in range(10):
        db_session.add(Member(first_name="M", last_name=str(i), membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    # 1. First partial payment ₹400 via checkout order
    order_res = await service.create_group_checkout_order(group.id, "2026-2027")
    mock_razorpay["fetch_pay"].return_value = {
        "id": "pay_accum_1",
        "order_id": order_res.order_id,
        "status": "captured",
        "amount": 40000,  # ₹400
        "currency": "INR",
        "method": "card",
    }
    await service.verify_group_checkout_payment(
        group.id,
        razorpay_order_id=order_res.order_id,
        razorpay_payment_id="pay_accum_1",
        razorpay_signature="sig_1",
    )

    detail1 = await service.get_group_obligation_detail(group.id, "2026-2027")
    assert detail1.obligation.collected_amount == 400.0
    assert detail1.obligation.pending_amount == 600.0
    assert detail1.obligation.status == GroupObligationStatus.PARTIAL.value

    # 2. Second payment for remaining ₹600 via Payment Link webhook
    mock_razorpay["link"].return_value = {
        "id": "plink_accum_2",
        "short_url": "https://rzp.io/i/accum2",
        "amount": 60000,
        "currency": "INR",
        "status": "created",
    }
    link_res = await service.create_group_payment_link(group.id, "2026-2027")
    assert link_res.amount == 600.0

    mock_razorpay["fetch_pay"].return_value = {
        "id": "pay_accum_2",
        "status": "captured",
        "amount": 60000,  # ₹600
        "currency": "INR",
        "method": "upi",
    }
    wh_payload = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_accum_2", "notes": {"obligation_id": str(obligation.id)}}},
            "payment": {"entity": {"id": "pay_accum_2"}},
        },
    }
    await service.handle_webhook(body=json.dumps(wh_payload).encode(), signature="sig_2")

    detail2 = await service.get_group_obligation_detail(group.id, "2026-2027")
    assert detail2.obligation.collected_amount == 1000.0
    assert detail2.obligation.pending_amount == 0.0
    assert detail2.obligation.status == GroupObligationStatus.PAID.value


@pytest.mark.asyncio
async def test_duplicate_webhook_idempotency(db_session, mock_razorpay):
    """Test Case 9 & 10: Duplicate webhook delivery does not double count."""
    group = SocialGroup(name="Idempotency Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    db_session.add(Member(first_name="M", last_name="1", membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = {
        "event_id": event_id,
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_idem_100",
                    "order_id": "order_idem_100",
                    "notes": {"obligation_id": str(obligation.id)},
                }
            }
        },
    }
    mock_razorpay["fetch_pay"].return_value = {
        "id": "pay_idem_100",
        "status": "captured",
        "amount": 10000,
        "currency": "INR",
        "method": "upi",
    }

    body = json.dumps(payload).encode()
    # First delivery
    res1 = await service.handle_webhook(body=body, signature="sig")
    assert res1["status"] == "processed"

    # Second delivery with exact same event_id
    res2 = await service.handle_webhook(body=body, signature="sig")
    assert res2["status"] == "already_processed"

    detail = await service.get_group_obligation_detail(group.id, "2026-2027")
    assert detail.obligation.collected_amount == 100.0  # NOT ₹200


@pytest.mark.asyncio
async def test_concurrent_overpayment_handled_safely(db_session, mock_razorpay):
    """Test Case 4 & 10: If concurrent payments exceed obligation, record as OVERPAID_REVIEW without discarding."""
    group = SocialGroup(name="Overpay Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    # 1 member = ₹100 expected
    db_session.add(Member(first_name="M", last_name="1", membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")

    # Payment 1: ₹100
    mock_razorpay["fetch_pay"].return_value = {"id": "pay_1", "status": "captured", "amount": 10000, "currency": "INR"}
    p1 = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_1", "notes": {"obligation_id": str(obligation.id)}}}},
    }
    await service.handle_webhook(body=json.dumps(p1).encode(), signature="sig")

    # Payment 2 arrives concurrently: another ₹100
    mock_razorpay["fetch_pay"].return_value = {"id": "pay_2", "status": "captured", "amount": 10000, "currency": "INR"}
    p2 = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_2", "notes": {"obligation_id": str(obligation.id)}}}},
    }
    await service.handle_webhook(body=json.dumps(p2).encode(), signature="sig")

    detail = await service.get_group_obligation_detail(group.id, "2026-2027")
    assert detail.obligation.collected_amount == 200.0
    assert detail.obligation.expected_amount == 100.0
    assert detail.obligation.status == GroupObligationStatus.OVERPAID_REVIEW.value
    assert len(detail.transactions) == 2


@pytest.mark.asyncio
async def test_fully_paid_group_disables_collection(db_session, mock_razorpay):
    """Test Case 11: Fully paid groups raise error on new payment attempt."""
    group = SocialGroup(name="Fully Paid Group", status="ACTIVE", is_active=True)
    db_session.add(group)
    await db_session.flush()

    db_session.add(Member(first_name="M", last_name="1", membership_status=MemberStatus.APPROVED.value, group_id=group.id))
    await db_session.flush()

    service = PaymentService(db_session)
    obligation = await service.get_or_create_group_obligation(group.id, "2026-2027")
    obligation.status = GroupObligationStatus.PAID.value
    obligation.pending_amount = Decimal("0.00")
    obligation.collected_amount = Decimal("100.00")
    await db_session.flush()

    with pytest.raises(BadRequestException, match="already fully paid"):
        await service.create_group_checkout_order(group.id, "2026-2027")

    with pytest.raises(BadRequestException, match="already fully paid"):
        await service.create_group_payment_link(group.id, "2026-2027")
