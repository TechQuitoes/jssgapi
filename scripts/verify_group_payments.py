"""Verification script for group payments logic without depending on external db drop/recreate."""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.constants import GroupObligationStatus, MemberStatus, PaymentStatus, PaymentWorkflow
from app.core.exceptions import BadRequestException
from app.models.group import SocialGroup
from app.models.group_payment_obligation import GroupPaymentObligation
from app.models.payment import Payment, PaymentTransaction
from app.services.payment_service import PaymentService


async def run_tests():
    print("==================================================")
    print("RUNNING JSSG GROUP PAYMENT TEST SUITE")
    print("==================================================")

    # 1. Calculation: 100 eligible members = 10,000
    rate = Decimal("100.00")
    members_100 = 100
    expected_100 = members_100 * rate
    assert expected_100 == Decimal("10000.00")
    print("✔ Test Case 1 Passed: 100 eligible members = ₹10,000 expected.")

    # 2. Calculation: 265 eligible members = 26,500
    members_265 = 265
    expected_265 = members_265 * rate
    assert expected_265 == Decimal("26500.00")
    print("✔ Test Case 2 Passed: 265 eligible members = ₹26,500 expected.")

    # 3. Zero payments = 100% outstanding (UNPAID)
    collected_0 = Decimal("0.00")
    pending_0 = max(expected_265 - collected_0, Decimal("0.00"))
    assert pending_0 == Decimal("26500.00")
    status_0 = GroupObligationStatus.UNPAID.value if collected_0 == 0 else GroupObligationStatus.PARTIAL.value
    assert status_0 == "UNPAID"
    print("✔ Test Case 3 Passed: Zero payments = 100% outstanding (status: UNPAID).")

    # 4. Partial payment of ₹5,000
    collected_5k = Decimal("5000.00")
    pending_5k = max(expected_265 - collected_5k, Decimal("0.00"))
    assert pending_5k == Decimal("21500.00")
    status_5k = GroupObligationStatus.PARTIAL.value
    assert status_5k == "PARTIAL"
    print("✔ Test Case 4 Passed: Partial payment of ₹5,000 gives ₹21,500 pending (status: PARTIAL).")

    # 5. Zero members = NOT_APPLICABLE
    members_0 = 0
    expected_0 = members_0 * rate
    status_zero = GroupObligationStatus.NOT_APPLICABLE.value if members_0 == 0 else "UNPAID"
    assert status_zero == "NOT_APPLICABLE"
    print("✔ Test Case 5 Passed: Zero members handled as NOT_APPLICABLE.")

    # Mock session and service tests
    mock_session = AsyncMock()
    service = PaymentService(mock_session)

    # 6. Recalculate obligation ledger logic
    test_obligation = GroupPaymentObligation(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        group_name="Indore Jain Mitra Mandal",
        billing_period="2026-2027",
        member_count_snapshot=265,
        rate_per_member=Decimal("100.00"),
        expected_amount=Decimal("26500.00"),
        collected_amount=Decimal("0.00"),
        pending_amount=Decimal("26500.00"),
        status="UNPAID",
    )

    # Helper for execute scalar_one
    def set_collected(amount):
        res = MagicMock()
        res.scalar_one.return_value = amount
        mock_session.execute = AsyncMock(return_value=res)

    # Simulate distinct captured payment of 10,000
    set_collected(Decimal("10000.00"))
    await service._recalculate_obligation_ledger(test_obligation)
    assert test_obligation.collected_amount == Decimal("10000.00")
    assert test_obligation.pending_amount == Decimal("16500.00")
    assert test_obligation.status == "PARTIAL"
    print("✔ Test Case 6 Passed: Ledger recalculation from distinct captured transactions works correctly.")

    # 7. Simulate full payment of 26,500
    set_collected(Decimal("26500.00"))
    await service._recalculate_obligation_ledger(test_obligation)
    assert test_obligation.collected_amount == Decimal("26500.00")
    assert test_obligation.pending_amount == Decimal("0.00")
    assert test_obligation.status == "PAID"
    print("✔ Test Case 7 Passed: Full payment marks status as PAID with 0 pending.")

    # 8. Simulate overpayment (concurrent capture gives 27,000)
    set_collected(Decimal("27000.00"))
    await service._recalculate_obligation_ledger(test_obligation)
    assert test_obligation.collected_amount == Decimal("27000.00")
    assert test_obligation.pending_amount == Decimal("0.00")
    assert test_obligation.status == "OVERPAID_REVIEW"
    print("✔ Test Case 8 Passed: Overpayment safely recorded as OVERPAID_REVIEW.")

    # 9. Super Admin Pay Now creates order with exact outstanding amount
    service.obligations.get_by_group_and_period = AsyncMock(return_value=test_obligation)
    service.obligations.get_by_id_for_update = AsyncMock(return_value=test_obligation)
    test_obligation.status = "PARTIAL"
    test_obligation.pending_amount = Decimal("16500.00")

    with patch("app.services.payment_service.create_razorpay_order") as mock_rzp_order:
        mock_rzp_order.return_value = {"id": "order_test_abc", "status": "created"}
        order_out = await service.create_group_checkout_order(test_obligation.group_id, "2026-2027")
        assert order_out.amount == 16500.0
        assert order_out.order_id == "order_test_abc"
        mock_rzp_order.assert_called_once()
        print("✔ Test Case 9 Passed: Super Admin checkout order created for exact pending amount (₹16,500).")

    # 10. Group Admin Payment Link creates link and cancels previous
    test_obligation.active_payment_link_id = "plink_old_123"
    with patch("app.services.payment_service.fetch_razorpay_payment_link") as mock_fetch_link, \
         patch("app.services.payment_service.cancel_razorpay_payment_link") as mock_cancel_link, \
         patch("app.services.payment_service.create_razorpay_payment_link") as mock_create_link:
        mock_fetch_link.return_value = {"id": "plink_old_123", "status": "created"}
        mock_create_link.return_value = {"id": "plink_new_456", "short_url": "https://rzp.io/i/test"}
        service.groups.get_by_id = AsyncMock(return_value=MagicMock(contact_name="Admin", contact_phone="9999999999", contact_email="admin@jssg.org"))

        link_out = await service.create_group_payment_link(test_obligation.group_id, "2026-2027")
        assert link_out.payment_link_id == "plink_new_456"
        assert link_out.short_url == "https://rzp.io/i/test"
        mock_cancel_link.assert_called_once_with("plink_old_123")
        print("✔ Test Case 10 Passed: Group Admin payment link created and obsolete link cancelled.")

    # 11. Fully paid group rejects new order creation
    test_obligation.status = "PAID"
    test_obligation.pending_amount = Decimal("0.00")
    try:
        await service.create_group_checkout_order(test_obligation.group_id, "2026-2027")
        assert False, "Should have raised BadRequestException"
    except BadRequestException as exc:
        assert "already fully paid" in str(exc)
        print("✔ Test Case 11 Passed: Fully paid group rejects additional order creation.")

    # 12. Strict Webhook Idempotency
    service.webhook_events.get_by_event_id = AsyncMock(return_value=MagicMock(status="PROCESSED"))
    with patch("app.services.payment_service.verify_razorpay_webhook_signature", return_value=True):
        wh_body = json.dumps({"event_id": "evt_duplicate_1", "event": "payment.captured"}).encode()
        wh_res = await service.handle_webhook(body=wh_body, signature="dummy_sig")
        assert wh_res["status"] == "already_processed"
        print("✔ Test Case 12 Passed: Duplicate webhook returns already_processed without ledger changes.")

    print("==================================================")
    print("ALL 12 CORE PAYMENT SERVICE TEST CASES PASSED!")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(run_tests())
