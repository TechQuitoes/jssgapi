from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.constants import (
    AuditAction,
    FeeStatus,
    GroupObligationStatus,
    PaymentStatus,
    PaymentWorkflow,
)
from app.core.exceptions import BadRequestException, ForbiddenException, NotFoundException
from app.integrations.razorpay import (
    cancel_razorpay_payment_link,
    create_razorpay_order,
    create_razorpay_payment_link,
    fetch_razorpay_payment,
    fetch_razorpay_payment_link,
    verify_razorpay_payment_signature,
    verify_razorpay_webhook_signature,
)
from app.models.fee import Fee
from app.models.group import SocialGroup
from app.models.group_payment_obligation import GroupPaymentObligation
from app.models.member import Member
from app.models.payment import Payment, PaymentTransaction
from app.models.webhook_event import WebhookEvent
from app.repositories.group_repository import GroupRepository
from app.repositories.obligation_repository import GroupPaymentObligationRepository
from app.repositories.payment_repository import (
    PaymentRepository,
    PaymentTransactionRepository,
)
from app.repositories.webhook_event_repository import WebhookEventRepository
from app.schemas.group_payment import (
    GroupObligationOut,
    GroupPaymentDetailOut,
    GroupPaymentLinkResponse,
    GroupPaymentOrderCreateResponse,
    GroupPaymentTransactionOut,
    PaymentSummaryOut,
)
from app.services.audit_service import AuditService
from app.utils.helpers import financial_year_for, format_to_dd_mm_yyyy

logger = logging.getLogger(__name__)
settings = get_settings()


class PaymentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.payments = PaymentRepository(session)
        self.transactions = PaymentTransactionRepository(session)
        self.obligations = GroupPaymentObligationRepository(session)
        self.webhook_events = WebhookEventRepository(session)
        self.groups = GroupRepository(session)
        self.audit = AuditService(session)

    # ── Group Payment Obligations & Ledger ──────────────────────────────

    async def _materialize_missing_obligations(self, period: str) -> None:
        """Batch-materialize initial obligation records for active groups if not yet created.

        Uses PostgreSQL ON CONFLICT DO NOTHING to ensure idempotency and concurrency safety.
        """
        active_groups_result = await self.session.execute(
            select(SocialGroup.id, SocialGroup.name).where(
                SocialGroup.is_deleted.is_(False),
                SocialGroup.is_active.is_(True),
            )
        )
        active_groups = active_groups_result.all()
        if not active_groups:
            return

        active_group_ids = [row[0] for row in active_groups]
        existing_result = await self.session.execute(
            select(GroupPaymentObligation.group_id).where(
                GroupPaymentObligation.billing_period == period,
                GroupPaymentObligation.group_id.in_(active_group_ids),
            )
        )
        existing_group_ids = set(row[0] for row in existing_result.all())
        missing = [g for g in active_groups if g[0] not in existing_group_ids]
        if not missing:
            return

        missing_ids = [g[0] for g in missing]
        counts_map = await self.groups.get_eligible_member_counts(missing_ids)
        rate = Decimal(str(settings.group_member_rate or 100.00))

        records_to_insert = []
        for gid, gname in missing:
            count = counts_map.get(gid, 0)
            expected_amt = Decimal(count) * rate
            initial_status = (
                GroupObligationStatus.NOT_APPLICABLE.value
                if count == 0
                else GroupObligationStatus.UNPAID.value
            )
            records_to_insert.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "billing_period": period,
                    "member_count_snapshot": count,
                    "rate_per_member": rate,
                    "expected_amount": expected_amt,
                    "collected_amount": Decimal("0.00"),
                    "pending_amount": expected_amt,
                    "status": initial_status,
                }
            )

        if records_to_insert:
            stmt = (
                pg_insert(GroupPaymentObligation)
                .values(records_to_insert)
                .on_conflict_do_nothing(index_elements=["group_id", "billing_period"])
            )
            await self.session.execute(stmt)
            await self.session.flush()

    async def get_or_create_group_obligation(
        self,
        group_id: uuid.UUID,
        billing_period: str | None = None,
    ) -> GroupPaymentObligation:
        """Fetch or create a group's payment obligation snapshot for the billing period.

        Member count snapshot and expected amount are recorded once and never silently altered.
        """
        period = billing_period or financial_year_for()
        obligation = await self.obligations.get_by_group_and_period(group_id, period)
        if obligation is not None:
            return obligation

        group = await self.groups.get_by_id(group_id)
        if group is None:
            raise NotFoundException("Group", str(group_id))

        # Calculate eligible members using distinct approved active members
        counts_map = await self.groups.get_eligible_member_counts([group_id])
        eligible_count = counts_map.get(group_id, 0)

        rate = Decimal(str(settings.group_member_rate or 100.00))
        expected = Decimal(eligible_count) * rate

        # Handle zero-member groups safely as NOT_APPLICABLE
        initial_status = (
            GroupObligationStatus.NOT_APPLICABLE.value
            if eligible_count == 0
            else GroupObligationStatus.UNPAID.value
        )

        stmt = (
            pg_insert(GroupPaymentObligation)
            .values(
                group_id=group_id,
                group_name=group.name,
                billing_period=period,
                member_count_snapshot=eligible_count,
                rate_per_member=rate,
                expected_amount=expected,
                collected_amount=Decimal("0.00"),
                pending_amount=expected,
                status=initial_status,
            )
            .on_conflict_do_nothing(index_elements=["group_id", "billing_period"])
        )
        await self.session.execute(stmt)
        await self.session.flush()

        obligation = await self.obligations.get_by_group_and_period(group_id, period)
        if obligation is None:
            raise NotFoundException("GroupPaymentObligation", f"{group_id}:{period}")
        return obligation

    async def get_payment_summary(
        self, billing_period: str | None = None
    ) -> PaymentSummaryOut:
        """Calculate global dashboard statistics from verified group payment obligations."""
        period = billing_period or financial_year_for()

        # Batch materialize any missing obligations safely
        await self._materialize_missing_obligations(period)

        # Aggregate totals across obligations for this period
        stats_stmt = select(
            func.coalesce(func.sum(GroupPaymentObligation.expected_amount), 0).label("expected"),
            func.coalesce(func.sum(GroupPaymentObligation.collected_amount), 0).label("collected"),
            func.coalesce(func.sum(GroupPaymentObligation.pending_amount), 0).label("outstanding"),
        ).where(GroupPaymentObligation.billing_period == period)

        stats_res = await self.session.execute(stats_stmt)
        expected, collected, outstanding = stats_res.one()

        # Count actual captured payment transactions
        tx_stmt = select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.status == PaymentStatus.SUCCESS.value
        )
        tx_count = (await self.session.execute(tx_stmt)).scalar_one()

        return PaymentSummaryOut(
            total_expected=float(expected),
            total_collected=float(collected),
            total_outstanding=float(outstanding),
            total_transactions=int(tx_count),
        )

    async def list_group_obligations(
        self,
        billing_period: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[GroupObligationOut], dict[str, Any]]:
        """Return paged group-wise payment obligations with collection metrics."""
        period = billing_period or financial_year_for()

        # Batch materialize any missing obligations safely
        await self._materialize_missing_obligations(period)

        offset = (page - 1) * page_size
        items, total = await self.obligations.list_for_period(
            period, search=search, limit=page_size, offset=offset
        )

        outputs = []
        for item in items:
            exp = float(item.expected_amount)
            col = float(item.collected_amount)
            pct = round((col / exp) * 100, 1) if exp > 0 else 0.0
            out = GroupObligationOut.model_validate(item)
            out.collection_percentage = pct
            if not out.due_date:
                out.due_date = "2027-03-31"
            outputs.append(out)

        pagination = {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 1,
        }
        return outputs, pagination

    async def get_group_obligation_detail(
        self, group_id: uuid.UUID, billing_period: str | None = None
    ) -> GroupPaymentDetailOut:
        """Fetch full group obligation ledger details and transaction history."""
        period = billing_period or financial_year_for()
        obligation = await self.get_or_create_group_obligation(group_id, period)

        exp = float(obligation.expected_amount)
        col = float(obligation.collected_amount)
        pct = round((col / exp) * 100, 1) if exp > 0 else 0.0

        obligation_out = GroupObligationOut.model_validate(obligation)
        obligation_out.collection_percentage = pct
        if not obligation_out.due_date:
            obligation_out.due_date = "2027-03-31"

        tx_list = await self.transactions.list_for_obligation(obligation.id)
        tx_outs = [GroupPaymentTransactionOut.model_validate(t) for t in tx_list]

        return GroupPaymentDetailOut(obligation=obligation_out, transactions=tx_outs)

    # ── WORKFLOW A: Super Admin Checkout ────────────────────────────────

    async def create_group_checkout_order(
        self,
        group_id: uuid.UUID,
        billing_period: str | None = None,
        amount: Decimal | float | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GroupPaymentOrderCreateResponse:
        """Create a Razorpay order for Super Admin checkout for full or custom amount."""
        period = billing_period or financial_year_for()
        obligation = await self.get_or_create_group_obligation(group_id, period)

        # Lock the obligation row
        locked_obligation = await self.obligations.get_by_id_for_update(obligation.id)
        if locked_obligation is None:
            raise NotFoundException("GroupPaymentObligation", str(obligation.id))

        if locked_obligation.status == GroupObligationStatus.PAID.value or locked_obligation.pending_amount <= 0:
            raise BadRequestException("Group obligation is already fully paid.")

        if locked_obligation.status == GroupObligationStatus.NOT_APPLICABLE.value:
            raise BadRequestException("Group has 0 eligible members and cannot accept payments.")

        pending_amount = Decimal(str(locked_obligation.pending_amount))
        if amount is not None and Decimal(str(amount)) > 0:
            custom_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
            if custom_amount > pending_amount:
                raise BadRequestException(f"Payment amount (₹{custom_amount}) cannot exceed pending amount (₹{pending_amount}).")
            amount_to_pay = custom_amount
        else:
            amount_to_pay = pending_amount
        receipt = f"JSSG-SA-{locked_obligation.group_id.hex[:6]}-{uuid.uuid4().hex[:6]}"

        razorpay_order = create_razorpay_order(
            amount=amount_to_pay,
            currency="INR",
            receipt=receipt,
            notes={
                "group_id": str(group_id),
                "obligation_id": str(locked_obligation.id),
                "workflow": PaymentWorkflow.SUPER_ADMIN_CHECKOUT.value,
            },
        )
        razorpay_order_id = razorpay_order["id"]

        payment = await self.payments.create(
            group_id=group_id,
            obligation_id=locked_obligation.id,
            order_id=razorpay_order_id,
            amount=amount_to_pay,
            currency="INR",
            status=PaymentStatus.PENDING.value,
            provider="razorpay",
            workflow=PaymentWorkflow.SUPER_ADMIN_CHECKOUT.value,
            provider_metadata=json.dumps(razorpay_order),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        await self.transactions.create_for_payment(
            payment,
            transaction_type="ORDER_CREATED",
            status=PaymentStatus.PENDING.value,
            group_id=group_id,
            obligation_id=locked_obligation.id,
            razorpay_order_id=razorpay_order_id,
            amount=amount_to_pay,
            payment_method="checkout",
        )

        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "group_id": str(group_id),
                "obligation_id": str(locked_obligation.id),
                "amount": str(amount_to_pay),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

        return GroupPaymentOrderCreateResponse(
            order_id=razorpay_order_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_key_id=settings.razorpay_key_id,
            amount=float(amount_to_pay),
            currency="INR",
            group_id=group_id,
            group_name=locked_obligation.group_name,
            obligation_id=locked_obligation.id,
            status=PaymentStatus.PENDING.value,
        )

    async def verify_group_checkout_payment(
        self,
        group_id: uuid.UUID,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> GroupPaymentDetailOut:
        """Verify Super Admin checkout response, capture payment via Razorpay API, and credit common ledger."""
        # 1. Signature verification
        is_valid = verify_razorpay_payment_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        if not is_valid:
            logger.warning(
                "Invalid checkout signature for order=%s payment=%s",
                razorpay_order_id,
                razorpay_payment_id,
            )
            raise BadRequestException("Invalid payment signature")

        # 2. Server-side payment verification directly with Razorpay API
        rz_payment = fetch_razorpay_payment(razorpay_payment_id)
        if rz_payment.get("status") != "captured":
            raise BadRequestException(
                f"Payment {razorpay_payment_id} is not captured on gateway (status: {rz_payment.get('status')})"
            )

        captured_amount = Decimal(str(rz_payment.get("amount", 0))) / Decimal(100)
        payment_method = rz_payment.get("method", "checkout")

        # 3. Find payment record and lock obligation row
        payment = await self.payments.get_by_order_id(razorpay_order_id)
        if payment is None:
            raise NotFoundException("Payment", razorpay_order_id)

        obligation = await self.obligations.get_by_id_for_update(payment.obligation_id)
        if obligation is None:
            raise NotFoundException("GroupPaymentObligation", str(payment.obligation_id))

        # 4. Strict idempotency check against duplicate crediting
        existing_tx = await self.transactions.get_by_razorpay_payment_id(razorpay_payment_id)
        if existing_tx is not None and existing_tx.status == PaymentStatus.SUCCESS.value:
            logger.info("Payment %s already verified and captured; returning existing ledger.", razorpay_payment_id)
            return await self.get_group_obligation_detail(group_id, obligation.billing_period)

        # 5. Update Payment record
        payment.status = PaymentStatus.SUCCESS.value
        payment.provider_reference = razorpay_payment_id
        payment.paid_at = datetime.now(UTC)
        payment.provider_metadata = json.dumps(rz_payment)

        # 6. Record PaymentTransaction
        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_CAPTURED",
            status=PaymentStatus.SUCCESS.value,
            group_id=group_id,
            obligation_id=obligation.id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            provider_transaction_id=razorpay_payment_id,
            payment_method=payment_method,
            amount=captured_amount,
            paid_at=datetime.now(UTC),
            gateway_response=json.dumps(rz_payment),
        )

        # 7. Atomically recalculate ledger from distinct captured transactions
        await self._recalculate_obligation_ledger(obligation)

        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "amount": str(captured_amount),
                "status": obligation.status,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        try:
            from app.services.notification_service import create_payment_notification

            await create_payment_notification(
                self.session,
                category="group_payment",
                amount=captured_amount,
                group_name=obligation.group_name or f"Group {group_id}",
                receipt_no=razorpay_payment_id,
                transaction_id=razorpay_payment_id,
                order_id=razorpay_order_id,
                recipient_id=user_id,
                payment_id=payment.id,
                link="/admin/payments",
            )
        except Exception as exc:
            logger.warning("Failed to create group checkout notification: %s", exc)

        await self.session.flush()
        return await self.get_group_obligation_detail(group_id, obligation.billing_period)

    # ── WORKFLOW B: Group Admin Payment Link ────────────────────────────

    async def create_group_payment_link(
        self,
        group_id: uuid.UUID,
        billing_period: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GroupPaymentLinkResponse:
        """Generate a Razorpay Payment Link for the group's current outstanding balance."""
        period = billing_period or financial_year_for()
        obligation = await self.get_or_create_group_obligation(group_id, period)

        locked_obligation = await self.obligations.get_by_id_for_update(obligation.id)
        if locked_obligation is None:
            raise NotFoundException("GroupPaymentObligation", str(obligation.id))

        if locked_obligation.status == GroupObligationStatus.PAID.value or locked_obligation.pending_amount <= 0:
            raise BadRequestException("Group obligation is already fully paid.")

        if locked_obligation.status == GroupObligationStatus.NOT_APPLICABLE.value:
            raise BadRequestException("Group has 0 eligible members and cannot accept payments.")

        # Single active link policy: Inspect and cancel prior active link if unpaid
        if locked_obligation.active_payment_link_id:
            try:
                prior_link = fetch_razorpay_payment_link(locked_obligation.active_payment_link_id)
                if prior_link.get("status") in ("created", "issued"):
                    cancel_razorpay_payment_link(locked_obligation.active_payment_link_id)
            except Exception as e:
                logger.warning("Could not check/cancel prior payment link %s: %s", locked_obligation.active_payment_link_id, e)

        amount_to_pay = Decimal(str(locked_obligation.pending_amount))
        group = await self.groups.get_by_id(group_id)

        customer = {}
        if group:
            if group.contact_name:
                customer["name"] = group.contact_name
            if group.contact_phone:
                customer["contact"] = group.contact_phone
            if group.contact_email:
                customer["email"] = group.contact_email

        link_response = create_razorpay_payment_link(
            amount=amount_to_pay,
            currency="INR",
            description=f"JSSG Annual Contribution - {locked_obligation.group_name}",
            customer=customer if customer else None,
            notes={
                "group_id": str(group_id),
                "obligation_id": str(locked_obligation.id),
                "billing_period": period,
                "workflow": PaymentWorkflow.GROUP_ADMIN_PAYMENT_LINK.value,
            },
        )

        payment_link_id = link_response["id"]
        short_url = link_response["short_url"]

        # Update obligation active link
        locked_obligation.active_payment_link_id = payment_link_id
        locked_obligation.active_payment_link_url = short_url

        # Persist internal payment record
        payment = await self.payments.create(
            group_id=group_id,
            obligation_id=locked_obligation.id,
            order_id=f"plink-{payment_link_id}",
            payment_link_id=payment_link_id,
            amount=amount_to_pay,
            currency="INR",
            status=PaymentStatus.PENDING.value,
            provider="razorpay",
            workflow=PaymentWorkflow.GROUP_ADMIN_PAYMENT_LINK.value,
            provider_metadata=json.dumps(link_response),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

        await self.transactions.create_for_payment(
            payment,
            transaction_type="LINK_CREATED",
            status=PaymentStatus.PENDING.value,
            group_id=group_id,
            obligation_id=locked_obligation.id,
            razorpay_payment_link_id=payment_link_id,
            amount=amount_to_pay,
            payment_method="payment_link",
        )

        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "payment_link_id": payment_link_id,
                "short_url": short_url,
                "group_id": str(group_id),
                "obligation_id": str(locked_obligation.id),
                "amount": str(amount_to_pay),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await self.session.flush()

        return GroupPaymentLinkResponse(
            payment_link_id=payment_link_id,
            short_url=short_url,
            amount=float(amount_to_pay),
            currency="INR",
            group_id=group_id,
            group_name=locked_obligation.group_name,
            obligation_id=locked_obligation.id,
            status="created",
        )

    # ── Webhook Handler ─────────────────────────────────────────────────

    async def handle_webhook(self, *, body: bytes, signature: str) -> dict:
        """Process incoming Razorpay webhook events with strict deduplication and API verification."""
        if not verify_razorpay_webhook_signature(body, signature):
            raise BadRequestException("Invalid webhook signature")

        payload = json.loads(body)
        event = payload.get("event", "")
        event_id = payload.get("event_id") or payload.get("id") or f"evt_{uuid.uuid4().hex[:12]}"

        logger.info("Razorpay webhook received: event=%s, event_id=%s", event, event_id)

        # 1. Strict event deduplication
        existing_event = await self.webhook_events.get_by_event_id(event_id)
        if existing_event is not None and existing_event.status == "PROCESSED":
            logger.info("Webhook event %s already processed; skipping.", event_id)
            return {"event": event, "status": "already_processed"}

        payment_id: str | None = None
        order_id: str | None = None
        payment_link_id: str | None = None

        try:
            if event == "payment.captured":
                payment_id, order_id = await self._handle_payment_captured(payload)
            elif event in ("payment_link.paid", "payment_link.partially_paid"):
                payment_link_id, payment_id = await self._handle_payment_link_paid(payload)
            elif event in ("payment_link.cancelled", "payment_link.expired"):
                payment_link_id = await self._handle_payment_link_closed(payload)
            elif event == "order.paid":
                order_id = await self._handle_order_paid(payload)
            elif event == "payment.failed":
                payment_id, order_id = await self._handle_payment_failed(payload)
            else:
                logger.info("Ignoring unhandled webhook event: %s", event)

            # Record successfully processed event
            if existing_event is not None:
                existing_event.status = "PROCESSED"
                existing_event.raw_payload = body.decode("utf-8", errors="ignore")
            else:
                await self.webhook_events.create(
                    event_id=event_id,
                    event_type=event,
                    payment_id=payment_id,
                    order_id=order_id,
                    payment_link_id=payment_link_id,
                    status="PROCESSED",
                    raw_payload=body.decode("utf-8", errors="ignore"),
                )
            await self.session.flush()
        except Exception as exc:
            logger.exception("Error processing webhook event %s: %s", event_id, exc)
            if existing_event is None:
                await self.webhook_events.create(
                    event_id=event_id,
                    event_type=event,
                    payment_id=payment_id,
                    order_id=order_id,
                    payment_link_id=payment_link_id,
                    status="FAILED",
                    raw_payload=body.decode("utf-8", errors="ignore"),
                )
                await self.session.flush()
            raise

        return {"event": event, "status": "processed"}

    async def _handle_payment_captured(self, payload: dict) -> tuple[str | None, str | None]:
        """Handle ``payment.captured`` event – verify status via Razorpay API and update ledger."""
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = entity.get("order_id")
        razorpay_payment_id = entity.get("id")
        notes = entity.get("notes", {}) or {}

        if not razorpay_payment_id:
            logger.warning("payment.captured webhook missing payment id")
            return None, None

        # Verify payment status and amount with Razorpay API
        rz_payment = fetch_razorpay_payment(razorpay_payment_id)
        if rz_payment.get("status") != "captured":
            logger.warning("payment.captured webhook: payment %s not captured on gateway", razorpay_payment_id)
            return razorpay_payment_id, razorpay_order_id

        captured_amount = Decimal(str(rz_payment.get("amount", 0))) / Decimal(100)
        payment_method = rz_payment.get("method", "webhook")

        # Resolve obligation
        obligation: GroupPaymentObligation | None = None
        obligation_id_str = notes.get("obligation_id")
        if obligation_id_str:
            try:
                obligation = await self.obligations.get_by_id_for_update(uuid.UUID(obligation_id_str))
            except Exception:
                obligation = None

        payment: Payment | None = None
        if razorpay_order_id:
            payment = await self.payments.get_by_order_id(razorpay_order_id)
            if payment and not obligation and payment.obligation_id:
                obligation = await self.obligations.get_by_id_for_update(payment.obligation_id)

        # Idempotency check against double counting
        existing_tx = await self.transactions.get_by_razorpay_payment_id(razorpay_payment_id)
        if existing_tx is not None and existing_tx.status == PaymentStatus.SUCCESS.value:
            logger.info("Webhook payment %s already processed; skipping ledger update.", razorpay_payment_id)
            return razorpay_payment_id, razorpay_order_id

        # Update or create payment row
        if payment is None:
            group_id = obligation.group_id if obligation else None
            payment = await self.payments.create(
                group_id=group_id,
                obligation_id=obligation.id if obligation else None,
                order_id=razorpay_order_id or f"direct-{razorpay_payment_id}",
                amount=captured_amount,
                currency="INR",
                status=PaymentStatus.SUCCESS.value,
                provider="razorpay",
                provider_reference=razorpay_payment_id,
                paid_at=datetime.now(UTC),
                provider_metadata=json.dumps(rz_payment),
            )
        else:
            payment.status = PaymentStatus.SUCCESS.value
            payment.provider_reference = razorpay_payment_id
            payment.paid_at = datetime.now(UTC)
            payment.provider_metadata = json.dumps(rz_payment)

        # Record payment transaction
        await self.transactions.create_for_payment(
            payment,
            transaction_type="WEBHOOK_CAPTURED",
            status=PaymentStatus.SUCCESS.value,
            group_id=obligation.group_id if obligation else payment.group_id,
            obligation_id=obligation.id if obligation else payment.obligation_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            provider_transaction_id=razorpay_payment_id,
            payment_method=payment_method,
            amount=captured_amount,
            paid_at=datetime.now(UTC),
            raw_response=json.dumps(payload),
        )

        if obligation is not None:
            await self._recalculate_obligation_ledger(obligation)

        # Mark fee as paid if fee_id is linked
        if payment.fee_id is not None:
            res = await self.session.execute(select(Fee).where(Fee.id == payment.fee_id))
            fee = res.scalar_one_or_none()
            if fee:
                fee.status = FeeStatus.PAID.value

        await self.session.flush()
        return razorpay_payment_id, razorpay_order_id

    async def _handle_payment_link_paid(self, payload: dict) -> tuple[str | None, str | None]:
        """Handle ``payment_link.paid`` / ``payment_link.partially_paid`` events."""
        plink_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        payment_link_id = plink_entity.get("id")
        razorpay_payment_id = payment_entity.get("id")
        notes = plink_entity.get("notes", {}) or {}

        if not payment_link_id:
            return None, None

        obligation: GroupPaymentObligation | None = None
        obligation_id_str = notes.get("obligation_id")
        if obligation_id_str:
            try:
                obligation = await self.obligations.get_by_id_for_update(uuid.UUID(obligation_id_str))
            except Exception:
                obligation = None

        if not obligation:
            obligation = await self.obligations.get_by_payment_link_id_for_update(payment_link_id)

        # If payment entity is included, process the capture directly
        if razorpay_payment_id:
            rz_payment = fetch_razorpay_payment(razorpay_payment_id)
            if rz_payment.get("status") == "captured":
                captured_amount = Decimal(str(rz_payment.get("amount", 0))) / Decimal(100)
                payment_method = rz_payment.get("method", "payment_link")

                existing_tx = await self.transactions.get_by_razorpay_payment_id(razorpay_payment_id)
                if existing_tx is None or existing_tx.status != PaymentStatus.SUCCESS.value:
                    payment = await self.payments.get_by_payment_link_id(payment_link_id)
                    if payment is None:
                        payment = await self.payments.create(
                            group_id=obligation.group_id if obligation else None,
                            obligation_id=obligation.id if obligation else None,
                            order_id=f"plink-{payment_link_id}-{razorpay_payment_id}",
                            payment_link_id=payment_link_id,
                            amount=captured_amount,
                            currency="INR",
                            status=PaymentStatus.SUCCESS.value,
                            provider="razorpay",
                            workflow=PaymentWorkflow.GROUP_ADMIN_PAYMENT_LINK.value,
                            provider_reference=razorpay_payment_id,
                            paid_at=datetime.now(UTC),
                            provider_metadata=json.dumps(rz_payment),
                        )
                    else:
                        payment.status = PaymentStatus.SUCCESS.value
                        payment.provider_reference = razorpay_payment_id
                        payment.paid_at = datetime.now(UTC)

                    await self.transactions.create_for_payment(
                        payment,
                        transaction_type="PAYMENT_LINK_CAPTURED",
                        status=PaymentStatus.SUCCESS.value,
                        group_id=obligation.group_id if obligation else None,
                        obligation_id=obligation.id if obligation else None,
                        razorpay_payment_id=razorpay_payment_id,
                        razorpay_payment_link_id=payment_link_id,
                        provider_transaction_id=razorpay_payment_id,
                        payment_method=payment_method,
                        amount=captured_amount,
                        paid_at=datetime.now(UTC),
                        raw_response=json.dumps(payload),
                    )

        if obligation is not None:
            await self._recalculate_obligation_ledger(obligation)
            # If obligation is paid, clear the active link
            if obligation.status == GroupObligationStatus.PAID.value:
                obligation.active_payment_link_id = None
                obligation.active_payment_link_url = None

        await self.session.flush()
        return payment_link_id, razorpay_payment_id

    async def _handle_payment_link_closed(self, payload: dict) -> str | None:
        """Clear active payment link on cancellation or expiry."""
        plink_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        payment_link_id = plink_entity.get("id")
        if not payment_link_id:
            return None

        obligation = await self.obligations.get_by_payment_link_id_for_update(payment_link_id)
        if obligation and obligation.active_payment_link_id == payment_link_id:
            obligation.active_payment_link_id = None
            obligation.active_payment_link_url = None
            await self.session.flush()

        return payment_link_id

    async def _handle_order_paid(self, payload: dict) -> str | None:
        """Handle ``order.paid`` backup event."""
        order_entity = payload.get("payload", {}).get("order", {}).get("entity", {})
        razorpay_order_id = order_entity.get("id")
        if not razorpay_order_id:
            return None

        payment = await self.payments.get_by_order_id(razorpay_order_id)
        if payment and payment.status == PaymentStatus.PENDING.value:
            payment.status = PaymentStatus.SUCCESS.value
            payment.paid_at = datetime.now(UTC)
            if payment.obligation_id:
                obligation = await self.obligations.get_by_id_for_update(payment.obligation_id)
                if obligation:
                    await self._recalculate_obligation_ledger(obligation)
            await self.session.flush()

        return razorpay_order_id

    async def _handle_payment_failed(self, payload: dict) -> tuple[str | None, str | None]:
        """Handle ``payment.failed`` event."""
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        razorpay_payment_id = payment_entity.get("id")

        if razorpay_order_id:
            payment = await self.payments.get_by_order_id(razorpay_order_id)
            if payment and payment.status == PaymentStatus.PENDING.value:
                payment.status = PaymentStatus.FAILED.value
                payment.provider_reference = razorpay_payment_id
                await self.transactions.create_for_payment(
                    payment,
                    transaction_type="WEBHOOK_FAILED",
                    status=PaymentStatus.FAILED.value,
                    group_id=payment.group_id,
                    obligation_id=payment.obligation_id,
                    razorpay_order_id=razorpay_order_id,
                    razorpay_payment_id=razorpay_payment_id,
                    provider_transaction_id=razorpay_payment_id,
                    amount=payment.amount,
                    raw_response=json.dumps(payload),
                )
                await self.session.flush()

        return razorpay_payment_id, razorpay_order_id

    # ── Concurrency & Ledger Calculation Helper ─────────────────────────

    async def _recalculate_obligation_ledger(self, obligation: GroupPaymentObligation) -> None:
        """Recalculate collected and pending amounts from distinct verified captured transactions.

        Handles overpayments safely by setting status to OVERPAID_REVIEW without discarding transactions.
        """
        stmt = select(
            func.coalesce(func.sum(PaymentTransaction.amount), 0)
        ).where(
            PaymentTransaction.obligation_id == obligation.id,
            PaymentTransaction.status == PaymentStatus.SUCCESS.value,
        )
        total_collected = (await self.session.execute(stmt)).scalar_one()
        obligation.collected_amount = Decimal(str(total_collected))

        expected = Decimal(str(obligation.expected_amount))
        collected = Decimal(str(obligation.collected_amount))

        if obligation.member_count_snapshot == 0:
            obligation.status = GroupObligationStatus.NOT_APPLICABLE.value
            obligation.pending_amount = Decimal("0.00")
        elif collected >= expected:
            obligation.pending_amount = Decimal("0.00")
            if collected > expected:
                obligation.status = GroupObligationStatus.OVERPAID_REVIEW.value
                logger.warning(
                    "Obligation %s OVERPAID: expected=₹%s, collected=₹%s",
                    obligation.id,
                    expected,
                    collected,
                )
            else:
                obligation.status = GroupObligationStatus.PAID.value
        elif collected > 0:
            obligation.pending_amount = max(expected - collected, Decimal("0.00"))
            obligation.status = GroupObligationStatus.PARTIAL.value
        else:
            obligation.pending_amount = expected
            obligation.status = GroupObligationStatus.UNPAID.value

        obligation.updated_at = datetime.now(UTC)

    # ── Payment Module Settings ─────────────────────────────────────────

    async def get_payment_settings(self) -> dict[str, Any]:
        """Fetch global payment module settings from app_settings or fallback defaults."""
        from sqlalchemy import text
        stmt = text("SELECT value FROM app_settings WHERE key = 'payment_settings' LIMIT 1;")
        res = await self.session.execute(stmt)
        row = res.scalar_one_or_none()
        defaults: dict[str, Any] = {
            "rate_per_member": float(settings.group_member_rate or 100.0),
            "billing_period": financial_year_for(),
            "due_date": "2027-03-31",
            "late_fee": 0.0,
            "allow_partial_payments": True,
            "min_partial_amount": 100.0,
            "payment_gateway": "Razorpay",
            "razorpay_key_id": settings.razorpay_key_id or "rzp_test_TzeUn0Iazq0yuq",
            "allowed_payment_methods": ["UPI", "Net Banking", "Debit/Credit Card"],
        }
        if row:
            if isinstance(row, dict):
                defaults.update(row)
            elif isinstance(row, str):
                import json
                try:
                    defaults.update(json.loads(row))
                except Exception:
                    pass
        return defaults

    async def update_payment_settings(self, new_settings: dict[str, Any]) -> dict[str, Any]:
        """Update payment module settings, and optionally apply to existing obligations."""
        from sqlalchemy import text
        from datetime import date
        import json
        current = await self.get_payment_settings()
        # Default apply_to_existing_obligations to True so that updating the rate or due date
        # immediately recalculates the group obligations for the active billing period.
        apply_existing = new_settings.pop("apply_to_existing_obligations", True)
        if apply_existing is None:
            apply_existing = True
        current.update(new_settings)

        stmt = text("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('payment_settings', CAST(:val AS jsonb), now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
        """)
        await self.session.execute(stmt, {"val": json.dumps(current)})

        if apply_existing:
            period = current.get("billing_period") or financial_year_for()
            due_date_raw = current.get("due_date", "2027-03-31")
            if isinstance(due_date_raw, str):
                try:
                    due_date_obj = date.fromisoformat(due_date_raw.strip())
                except Exception:
                    due_date_obj = date(2027, 3, 31)
            elif isinstance(due_date_raw, date):
                due_date_obj = due_date_raw
            else:
                due_date_obj = date(2027, 3, 31)

            new_rate = Decimal(str(current.get("rate_per_member", 100.0)))

            update_stmt = text("""
                UPDATE group_payment_obligations
                SET due_date = :due_date,
                    rate_per_member = CAST(:rate AS numeric),
                    expected_amount = member_count_snapshot * CAST(:rate AS numeric),
                    pending_amount = GREATEST(0, (member_count_snapshot * CAST(:rate AS numeric)) - collected_amount),
                    status = CASE 
                        WHEN member_count_snapshot = 0 THEN 'NOT_APPLICABLE'
                        WHEN collected_amount >= (member_count_snapshot * CAST(:rate AS numeric)) THEN 'PAID'
                        WHEN collected_amount > 0 THEN 'PARTIAL'
                        ELSE 'UNPAID'
                    END,
                    updated_at = now()
                WHERE billing_period = :period;
            """)
            await self.session.execute(update_stmt, {
                "due_date": due_date_obj,
                "rate": new_rate,
                "period": period,
            })

        await self.session.commit()
        return current

    # ── Legacy Individual Payment Methods ────────────────────────────────

    async def create_order(
        self,
        *,
        member_id: uuid.UUID | None,
        fee_id: uuid.UUID | None,
        amount: Decimal,
        currency: str = "INR",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Payment:
        if member_id is not None:
            res = await self.session.execute(select(Member).where(Member.id == member_id))
            if res.scalar_one_or_none() is None:
                raise NotFoundException("Member", str(member_id))

        if fee_id is not None:
            res = await self.session.execute(select(Fee).where(Fee.id == fee_id))
            if res.scalar_one_or_none() is None:
                raise NotFoundException("Fee", str(fee_id))

        receipt = f"JSSG-{uuid.uuid4().hex.upper()[:16]}"
        razorpay_order = create_razorpay_order(
            amount=amount,
            currency=currency,
            receipt=receipt,
            notes={
                "member_id": str(member_id) if member_id else "",
                "fee_id": str(fee_id) if fee_id else "",
            },
        )
        razorpay_order_id = razorpay_order["id"]

        payment = await self.payments.create(
            member_id=member_id,
            fee_id=fee_id,
            order_id=razorpay_order_id,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING.value,
            provider="razorpay",
            provider_metadata=json.dumps(razorpay_order),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        await self.transactions.create_for_payment(
            payment, transaction_type="ORDER_CREATED", status="PENDING"
        )
        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            details={"order_id": razorpay_order_id, "amount": str(amount)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return payment

    async def verify_payment(
        self,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Payment:
        payment = await self.payments.get_by_order_id(razorpay_order_id)
        if payment is None:
            raise NotFoundException("Payment", razorpay_order_id)

        if payment.status != PaymentStatus.PENDING.value:
            raise BadRequestException(f"Cannot verify a {payment.status} payment")

        is_valid = verify_razorpay_payment_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )

        if is_valid:
            payment.status = PaymentStatus.SUCCESS.value
            payment.provider_reference = razorpay_payment_id
            payment.paid_at = datetime.now(UTC)
            try:
                rz_payment = fetch_razorpay_payment(razorpay_payment_id)
                payment.provider_metadata = json.dumps(rz_payment)
            except Exception:
                logger.exception("Failed to fetch Razorpay details for %s", razorpay_payment_id)

            if payment.fee_id is not None:
                res = await self.session.execute(select(Fee).where(Fee.id == payment.fee_id))
                fee = res.scalar_one_or_none()
                if fee is not None:
                    fee.status = FeeStatus.PAID.value

            final_status = PaymentStatus.SUCCESS
        else:
            payment.status = PaymentStatus.FAILED.value
            final_status = PaymentStatus.FAILED

        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_VERIFY",
            status=final_status.value,
            provider_transaction_id=razorpay_payment_id,
            amount=payment.amount,
        )
        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={"order_id": razorpay_order_id, "signature_valid": is_valid},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(payment)
        return payment

    # ── Flutter Group Fee Payment Flow ────────────────────────────────────

    async def get_group_fee_status(
        self,
        group_id: uuid.UUID,
        user: Any,
        billing_period: str | None = None,
    ) -> dict[str, Any]:
        """Fetch group fee status & calculation for the Flutter app.

        Returns fee breakdown (members × rate), payment status, and whether
        the current authenticated user is an admin of this group.
        """
        from app.core.constants import RoleName, resolve_role_name
        from app.schemas.group_fee import GroupFeeStatusResponse

        period = billing_period or financial_year_for()

        # Fetch group
        group = await self.groups.get_by_id(group_id)
        if group is None:
            raise NotFoundException("Group", str(group_id))

        # Determine if user is an admin of this specific group
        is_admin = False
        if user and user.role:
            role = resolve_role_name(user.role.name)
            if role == RoleName.SUPER_ADMIN:
                is_admin = True
            elif role == RoleName.GROUP_ADMIN and user.group_id == group_id:
                is_admin = True

        # Get or create the obligation (contains member count, rate, amounts)
        obligation = await self.get_or_create_group_obligation(group_id, period)

        # Try to find the fee record for this group and period
        fee_result = await self.session.execute(
            select(Fee).where(
                Fee.group_id == group_id,
                Fee.financial_year == period,
                Fee.is_deleted.is_(False),
            ).order_by(Fee.due_date.desc()).limit(1)
        )
        fee = fee_result.scalar_one_or_none()

        # Map obligation status to a simpler fee status for the app
        status_map = {
            GroupObligationStatus.UNPAID.value: "PENDING",
            GroupObligationStatus.PARTIAL.value: "PENDING",
            GroupObligationStatus.PAID.value: "PAID",
            GroupObligationStatus.NOT_APPLICABLE.value: "PAID",
            GroupObligationStatus.OVERPAID_REVIEW.value: "PAID",
        }
        fee_status = status_map.get(obligation.status, "PENDING")

        # If fee exists and has a due date, check for overdue
        due_date_str: str | None = None
        if fee and fee.due_date:
            due_date_str = format_to_dd_mm_yyyy(fee.due_date)
            from datetime import date as date_type
            if fee_status == "PENDING" and fee.due_date < date_type.today():
                fee_status = "OVERDUE"

        response = GroupFeeStatusResponse(
            fee_id=fee.id if fee else None,
            obligation_id=obligation.id,
            group_id=group_id,
            group_name=group.name,
            financial_year=period,
            fee_per_member=float(obligation.rate_per_member),
            active_member_count=obligation.member_count_snapshot,
            total_amount=float(obligation.expected_amount),
            collected_amount=float(obligation.collected_amount),
            pending_amount=float(obligation.pending_amount),
            due_date=due_date_str,
            status=fee_status,
            is_admin=is_admin,
            currency="INR",
        )
        return response.model_dump(mode="json")

    async def create_group_fee_order(
        self,
        *,
        group_id: uuid.UUID,
        fee_id: uuid.UUID | None = None,
        user: Any,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay order for group fee payment (Flutter app flow).

        Security invariants:
        - Only verified Group Admins of this specific group (or Super Admin) can create orders.
        - Amount is ALWAYS calculated server-side (never from client).
        - Only razorpay_key_id (public key) is returned, never the secret.
        """
        from app.core.constants import RoleName, resolve_role_name
        from app.schemas.group_fee import GroupFeeOrderResponse

        # 1. Verify admin access
        if not user or not user.role:
            raise ForbiddenException("Authentication required")
        role = resolve_role_name(user.role.name)
        if role == RoleName.GROUP_ADMIN:
            if user.group_id != group_id:
                raise ForbiddenException("You are not an admin of this group")
        elif role != RoleName.SUPER_ADMIN:
            raise ForbiddenException("Only Group Admins or Super Admins can initiate payments")

        period = financial_year_for()

        # 2. Get or create obligation & lock
        obligation = await self.get_or_create_group_obligation(group_id, period)
        locked = await self.obligations.get_by_id_for_update(obligation.id)
        if locked is None:
            raise NotFoundException("GroupPaymentObligation", str(obligation.id))

        # 3. Verify fee is not already PAID
        if locked.status == GroupObligationStatus.PAID.value or locked.pending_amount <= 0:
            raise BadRequestException("Fee is already PAID for this group and period")

        if locked.status == GroupObligationStatus.NOT_APPLICABLE.value:
            raise BadRequestException("Group has 0 eligible members; no fee to pay")

        # 4. Re-calculate total amount server-side
        counts_map = await self.groups.get_eligible_member_counts([group_id])
        member_count = counts_map.get(group_id, 0)
        rate = Decimal(str(settings.group_member_rate or 100.00))
        total_amount = Decimal(member_count) * rate
        amount_to_pay = min(total_amount, Decimal(str(locked.pending_amount)))

        if amount_to_pay <= 0:
            raise BadRequestException("No outstanding amount to pay")

        # 5. Convert to paise
        amount_in_paise = int(amount_to_pay * 100)

        # 6. Create Razorpay order
        receipt = f"fee_{group_id}_{period}"
        notes = {
            "group_id": str(group_id),
            "obligation_id": str(locked.id),
            "workflow": PaymentWorkflow.GROUP_ADMIN_PAYMENT_LINK.value,
        }
        if fee_id:
            notes["fee_id"] = str(fee_id)

        razorpay_order = create_razorpay_order(
            amount=amount_to_pay,
            currency="INR",
            receipt=receipt,
            notes=notes,
        )
        razorpay_order_id = razorpay_order["id"]

        # 7. Create payment record
        payment = await self.payments.create(
            group_id=group_id,
            obligation_id=locked.id,
            fee_id=fee_id,
            order_id=razorpay_order_id,
            amount=amount_to_pay,
            currency="INR",
            status=PaymentStatus.PENDING.value,
            provider="razorpay",
            workflow=PaymentWorkflow.GROUP_ADMIN_PAYMENT_LINK.value,
            provider_metadata=json.dumps(razorpay_order),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        await self.transactions.create_for_payment(
            payment,
            transaction_type="ORDER_CREATED",
            status=PaymentStatus.PENDING.value,
            group_id=group_id,
            obligation_id=locked.id,
            razorpay_order_id=razorpay_order_id,
            amount=amount_to_pay,
            payment_method="checkout",
        )

        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "group_id": str(group_id),
                "obligation_id": str(locked.id),
                "amount": str(amount_to_pay),
                "workflow": "GROUP_ADMIN_FLUTTER_CHECKOUT",
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

        response = GroupFeeOrderResponse(
            payment_id=payment.id,
            razorpay_order_id=razorpay_order_id,
            amount=amount_in_paise,
            currency="INR",
            razorpay_key_id=settings.razorpay_key_id,
            group_id=group_id,
            group_name=locked.group_name,
        )
        return response.model_dump(mode="json")

    async def verify_group_fee_payment(
        self,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        payment_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Verify Razorpay payment signature for group fee payment (Flutter app flow).

        Security: Verifies HMAC-SHA256 signature using the server-side key secret.
        On success, updates payment + obligation ledger + fee record.
        """
        from app.schemas.group_fee import GroupFeeVerifyResponse

        # 1. Verify cryptographic signature
        is_valid = verify_razorpay_payment_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        if not is_valid:
            logger.warning(
                "Invalid payment signature for order=%s payment=%s",
                razorpay_order_id,
                razorpay_payment_id,
            )
            raise BadRequestException("Invalid payment signature")

        # 2. Find payment by order_id (or payment_id)
        payment = await self.payments.get_by_order_id(razorpay_order_id)
        if payment is None and payment_id:
            payment = await self.payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundException("Payment", razorpay_order_id)

        # 3. Idempotency: if already verified, return success
        if payment.status == PaymentStatus.SUCCESS.value:
            response = GroupFeeVerifyResponse(
                status="SUCCESS",
                message="Payment already verified",
                transaction_id=payment.provider_reference or razorpay_payment_id,
                paid_at=payment.paid_at,
                receipt_url=None,
            )
            return response.model_dump(mode="json")

        if payment.status != PaymentStatus.PENDING.value:
            raise BadRequestException(f"Cannot verify a {payment.status} payment")

        # 4. Server-side verification with Razorpay API
        rz_payment = fetch_razorpay_payment(razorpay_payment_id)
        if rz_payment.get("status") != "captured":
            raise BadRequestException(
                f"Payment {razorpay_payment_id} is not captured on gateway "
                f"(status: {rz_payment.get('status')})"
            )

        captured_amount = Decimal(str(rz_payment.get("amount", 0))) / Decimal(100)
        payment_method = rz_payment.get("method", "checkout")
        paid_at = datetime.now(UTC)

        # 5. Update payment record
        payment.status = PaymentStatus.SUCCESS.value
        payment.provider_reference = razorpay_payment_id
        payment.paid_at = paid_at
        payment.provider_metadata = json.dumps(rz_payment)

        # 6. Record transaction
        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_CAPTURED",
            status=PaymentStatus.SUCCESS.value,
            group_id=payment.group_id,
            obligation_id=payment.obligation_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            provider_transaction_id=razorpay_payment_id,
            payment_method=payment_method,
            amount=captured_amount,
            paid_at=paid_at,
            gateway_response=json.dumps(rz_payment),
        )

        # 7. Update obligation ledger
        if payment.obligation_id:
            obligation = await self.obligations.get_by_id_for_update(payment.obligation_id)
            if obligation:
                await self._recalculate_obligation_ledger(obligation)

        # 8. Update fee record to PAID
        if payment.fee_id:
            fee_result = await self.session.execute(
                select(Fee).where(Fee.id == payment.fee_id)
            )
            fee = fee_result.scalar_one_or_none()
            if fee:
                fee.status = FeeStatus.PAID.value

        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "amount": str(captured_amount),
                "workflow": "GROUP_ADMIN_FLUTTER_CHECKOUT",
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        try:
            from app.services.notification_service import create_payment_notification

            group_name = None
            if payment.group_id:
                grp = await self.groups.get_by_id(payment.group_id)
                if grp:
                    group_name = grp.name

            await create_payment_notification(
                self.session,
                category="group_payment",
                amount=captured_amount,
                group_name=group_name,
                receipt_no=razorpay_payment_id,
                transaction_id=razorpay_payment_id,
                order_id=razorpay_order_id,
                recipient_id=user_id,
                payment_id=payment.id,
                sent_at=paid_at,
                link="/admin/payments",
            )
        except Exception as exc:
            logger.warning("Failed to create group fee notification: %s", exc)

        await self.session.flush()

        response = GroupFeeVerifyResponse(
            status="SUCCESS",
            message="Payment verified successfully",
            transaction_id=razorpay_payment_id,
            paid_at=paid_at,
            receipt_url=None,  # Receipt generation can be added later
        )
        return response.model_dump(mode="json")

    async def get_group_payment_history(
        self,
        group_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return list of past successful fee payments for a group.

        Used by GET /groups/{group_id}/payments in the Flutter app.
        """
        from app.schemas.group_fee import GroupPaymentHistoryItem

        # Fetch all successful payments for this group
        stmt = (
            select(Payment)
            .where(
                Payment.group_id == group_id,
                Payment.status == PaymentStatus.SUCCESS.value,
            )
            .order_by(Payment.paid_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        payments = list(result.scalars().all())

        items: list[dict[str, Any]] = []
        for p in payments:
            # Try to get financial year from linked obligation or fee
            fy = ""
            if p.obligation_id:
                obl_result = await self.session.execute(
                    select(GroupPaymentObligation.billing_period).where(
                        GroupPaymentObligation.id == p.obligation_id
                    )
                )
                row = obl_result.scalar_one_or_none()
                if row:
                    fy = row
            if not fy and p.fee_id:
                fee_result = await self.session.execute(
                    select(Fee.financial_year).where(Fee.id == p.fee_id)
                )
                row = fee_result.scalar_one_or_none()
                if row:
                    fy = row
            if not fy:
                fy = financial_year_for(p.paid_at) if p.paid_at else financial_year_for()

            # Get payment method from latest transaction
            tx_result = await self.session.execute(
                select(PaymentTransaction.payment_method).where(
                    PaymentTransaction.payment_id == p.id,
                    PaymentTransaction.status == PaymentStatus.SUCCESS.value,
                ).order_by(PaymentTransaction.created_at.desc()).limit(1)
            )
            method = tx_result.scalar_one_or_none()

            item = GroupPaymentHistoryItem(
                payment_id=p.id,
                financial_year=fy,
                amount=float(p.amount),
                currency=p.currency,
                status=p.status,
                transaction_id=p.provider_reference,
                paid_at=p.paid_at,
                receipt_url=None,
                payment_method=method,
                created_at=p.created_at,
            )
            items.append(item.model_dump(mode="json"))

        return items

    async def verify(
        self,
        payment: Payment,
        *,
        provider_reference: str,
        status: PaymentStatus,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Payment:
        if payment.status != PaymentStatus.PENDING.value:
            raise BadRequestException(f"Cannot verify a {payment.status} payment")

        final = status if status == PaymentStatus.SUCCESS else PaymentStatus.FAILED
        payment.status = final.value
        payment.provider_reference = provider_reference
        if final == PaymentStatus.SUCCESS:
            payment.paid_at = datetime.now(UTC)

        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_VERIFY",
            status=final.value,
            provider_transaction_id=provider_reference,
            amount=payment.amount,
        )
        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={"order_id": payment.order_id, "status": final.value},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(payment)
        return payment

    # ── Voluntary Contribution / Donation Flow ────────────────────────────

    async def create_contribution_order(
        self,
        *,
        amount: Decimal,
        user: Any,
        group_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        purpose: str | None = None,
        notes: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay order for voluntary contribution (no obligation lock).

        Anyone (MEMBER, GROUP_ADMIN, SUPER_ADMIN) can contribute any custom amount,
        multiple times, without being blocked by previous payments.
        """
        from app.schemas.payment import ContributionOrderResponse

        if not user:
            raise ForbiddenException("Authentication required")

        if amount <= Decimal("0"):
            raise BadRequestException("Contribution amount must be greater than 0")

        # Optional group check
        effective_group_id = group_id or getattr(user, "group_id", None)
        if effective_group_id is not None:
            grp = await self.groups.get_by_id(effective_group_id)
            if grp is None:
                if group_id is not None:
                    raise NotFoundException("Group", str(group_id))
                effective_group_id = None

        # Optional member check (link if this user has an associated member profile)
        member_id: uuid.UUID | None = None
        user_id = getattr(user, "id", None)
        if user_id is not None:
            member_stmt = select(Member.id).where(Member.user_id == user_id).limit(1)
            member_res = await self.session.execute(member_stmt)
            member_id = member_res.scalar_one_or_none()

        receipt = f"cnt_{uuid.uuid4().hex[:14]}"
        amount_in_paise = int(amount * 100)

        order_notes = {
            "workflow": PaymentWorkflow.MEMBER_CONTRIBUTION.value,
            "user_id": str(user.id),
            "purpose": (purpose or "General Contribution")[:100],
        }
        if effective_group_id:
            order_notes["group_id"] = str(effective_group_id)
        if event_id:
            order_notes["event_id"] = str(event_id)

        razorpay_order = create_razorpay_order(
            amount=amount,
            currency="INR",
            receipt=receipt,
            notes=order_notes,
        )
        razorpay_order_id = razorpay_order["id"]

        metadata = {
            "razorpay_order": razorpay_order,
            "purpose": purpose,
            "notes": notes,
            "event_id": str(event_id) if event_id else None,
            "donor_user_id": str(user.id),
            "donor_name": getattr(user, "full_name", None),
            "donor_phone": getattr(user, "phone_number", None),
        }

        payment = await self.payments.create(
            member_id=member_id,
            group_id=effective_group_id,
            obligation_id=None,
            fee_id=None,
            order_id=razorpay_order_id,
            amount=amount,
            currency="INR",
            status=PaymentStatus.PENDING.value,
            provider="razorpay",
            workflow=PaymentWorkflow.MEMBER_CONTRIBUTION.value,
            provider_metadata=json.dumps(metadata),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

        await self.transactions.create_for_payment(
            payment,
            transaction_type="ORDER_CREATED",
            status=PaymentStatus.PENDING.value,
            group_id=effective_group_id,
            razorpay_order_id=razorpay_order_id,
            amount=amount,
            payment_method="checkout",
        )

        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "amount": str(amount),
                "workflow": PaymentWorkflow.MEMBER_CONTRIBUTION.value,
                "purpose": purpose,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

        response = ContributionOrderResponse(
            payment_id=payment.id,
            order_id=payment.order_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_key_id=settings.razorpay_key_id,
            amount=amount_in_paise,
            amount_in_rupees=amount,
            currency="INR",
            group_id=effective_group_id,
            purpose=purpose,
        )
        return response.model_dump(mode="json")

    async def verify_contribution_payment(
        self,
        *,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        payment_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Verify Razorpay payment signature for voluntary contribution."""
        from app.schemas.payment import ContributionVerifyResponse

        # 1. Verify HMAC-SHA256 signature
        is_valid = verify_razorpay_payment_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        if not is_valid:
            logger.warning(
                "Invalid payment signature for contribution order=%s payment=%s",
                razorpay_order_id,
                razorpay_payment_id,
            )
            raise BadRequestException("Invalid payment signature")

        # 2. Find payment by order_id or payment_id
        payment = await self.payments.get_by_order_id(razorpay_order_id)
        if payment is None and payment_id:
            payment = await self.payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundException("Payment", razorpay_order_id)

        # Extract metadata
        purpose = None
        donor_name = None
        if payment.provider_metadata:
            try:
                meta = json.loads(payment.provider_metadata)
                purpose = meta.get("purpose")
                donor_name = meta.get("donor_name")
            except Exception:
                pass

        # 3. Idempotency check: if already success, return immediately
        if payment.status == PaymentStatus.SUCCESS.value:
            response = ContributionVerifyResponse(
                status="SUCCESS",
                message="Contribution already verified",
                transaction_id=payment.provider_reference or razorpay_payment_id,
                amount=payment.amount,
                currency=payment.currency,
                paid_at=payment.paid_at,
                purpose=purpose,
                donor_name=donor_name,
                receipt_url=None,
            )
            return response.model_dump(mode="json")

        if payment.status != PaymentStatus.PENDING.value:
            raise BadRequestException(f"Cannot verify a {payment.status} payment")

        # 4. Gateway check
        rz_payment = fetch_razorpay_payment(razorpay_payment_id)
        if rz_payment.get("status") != "captured":
            raise BadRequestException(
                f"Payment {razorpay_payment_id} is not captured on gateway "
                f"(status: {rz_payment.get('status')})"
            )

        captured_amount = Decimal(str(rz_payment.get("amount", 0))) / Decimal(100)
        payment_method = rz_payment.get("method", "checkout")
        paid_at = datetime.now(UTC)

        # 5. Update payment record
        payment.status = PaymentStatus.SUCCESS.value
        payment.provider_reference = razorpay_payment_id
        payment.paid_at = paid_at

        try:
            current_meta = json.loads(payment.provider_metadata or "{}")
        except Exception:
            current_meta = {}
        current_meta["razorpay_payment"] = rz_payment
        payment.provider_metadata = json.dumps(current_meta)

        # 6. Record transaction
        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_CAPTURED",
            status=PaymentStatus.SUCCESS.value,
            group_id=payment.group_id,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            provider_transaction_id=razorpay_payment_id,
            payment_method=payment_method,
            amount=captured_amount,
            paid_at=paid_at,
            gateway_response=json.dumps(rz_payment),
        )

        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "amount": str(captured_amount),
                "workflow": PaymentWorkflow.MEMBER_CONTRIBUTION.value,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        try:
            from app.services.notification_service import create_payment_notification

            group_name = None
            if payment.group_id:
                grp = await self.groups.get_by_id(payment.group_id)
                if grp:
                    group_name = grp.name

            member_user_id = user_id
            if not member_user_id and payment.member_id:
                m_res = await self.session.execute(select(Member).where(Member.id == payment.member_id).limit(1))
                m = m_res.scalar_one_or_none()
                if m:
                    member_user_id = m.user_id
                    if not donor_name:
                        donor_name = f"{m.first_name} {m.last_name or ''}".strip()

            if not member_user_id and current_meta and current_meta.get("donor_user_id"):
                try:
                    member_user_id = uuid.UUID(str(current_meta.get("donor_user_id")))
                except Exception:
                    pass

            if not donor_name and current_meta and current_meta.get("donor_name"):
                donor_name = current_meta.get("donor_name")

            await create_payment_notification(
                self.session,
                category="contribution",
                amount=captured_amount,
                donor_name=donor_name,
                group_name=group_name,
                purpose=purpose or "Voluntary Contribution",
                receipt_no=razorpay_payment_id,
                transaction_id=razorpay_payment_id,
                order_id=razorpay_order_id,
                recipient_id=member_user_id,
                payment_id=payment.id,
                sent_at=paid_at,
                link="/admin/contributions",
            )
        except Exception as exc:
            logger.warning("Failed to create contribution notification: %s", exc)

        await self.session.flush()

        response = ContributionVerifyResponse(
            status="SUCCESS",
            message="Contribution payment verified successfully",
            transaction_id=razorpay_payment_id,
            amount=captured_amount,
            currency=payment.currency,
            paid_at=paid_at,
            purpose=purpose,
            donor_name=donor_name,
            receipt_url=None,
        )
        return response.model_dump(mode="json")

    async def list_contributions(
        self,
        *,
        user: Any,
        group_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """List past successful voluntary contributions."""
        from app.core.constants import RoleName, resolve_role_name
        from app.schemas.payment import ContributionHistoryItem

        role = resolve_role_name(user.role.name) if user and user.role else RoleName.MEMBER

        stmt = select(Payment).where(
            Payment.workflow == PaymentWorkflow.MEMBER_CONTRIBUTION.value,
            Payment.status == PaymentStatus.SUCCESS.value,
        )

        if role == RoleName.SUPER_ADMIN:
            if group_id:
                stmt = stmt.where(Payment.group_id == group_id)
        elif role == RoleName.GROUP_ADMIN:
            effective_group_id = group_id or user.group_id
            stmt = stmt.where(Payment.group_id == effective_group_id)
        else:
            # MEMBER: filter to payments linked to their member id or group
            member_id: uuid.UUID | None = None
            user_id = getattr(user, "id", None)
            if user_id is not None:
                member_stmt = select(Member.id).where(Member.user_id == user_id).limit(1)
                member_res = await self.session.execute(member_stmt)
                member_id = member_res.scalar_one_or_none()

            if member_id is not None:
                stmt = stmt.where(Payment.member_id == member_id)
            elif getattr(user, "group_id", None):
                stmt = stmt.where(Payment.group_id == user.group_id)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        offset = (page - 1) * page_size
        stmt = (
            stmt.options(selectinload(Payment.group), selectinload(Payment.member))
            .order_by(Payment.paid_at.desc().nullslast(), Payment.created_at.desc())
            .limit(page_size)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        payments = list(result.scalars().all())

        items: list[dict[str, Any]] = []
        for p in payments:
            purpose = None
            notes = None
            donor_name = None
            donor_phone = None
            payment_method = None
            receipt_no = p.order_id
            if p.provider_metadata:
                try:
                    meta = json.loads(p.provider_metadata) if isinstance(p.provider_metadata, str) else p.provider_metadata
                    purpose = meta.get("purpose")
                    notes = meta.get("notes")
                    donor_name = meta.get("donor_name")
                    donor_phone = meta.get("donor_phone")
                    payment_method = meta.get("payment_method")
                    receipt_no = meta.get("receipt_no") or p.order_id
                except Exception:
                    pass

            group_name = p.group.name if p.group else None
            member_name = None
            if p.member:
                member_name = f"{p.member.first_name} {p.member.last_name or ''}".strip()
                if not donor_phone and hasattr(p.member, "contact_phone"):
                    donor_phone = p.member.contact_phone
            elif donor_name:
                member_name = donor_name

            item = ContributionHistoryItem(
                id=p.id,
                order_id=p.order_id,
                transaction_id=p.provider_reference,
                receipt_no=receipt_no,
                amount=p.amount,
                currency=p.currency,
                status=p.status,
                group_id=p.group_id,
                group_name=group_name,
                member_id=p.member_id,
                member_name=member_name,
                donor_name=donor_name or member_name,
                donor_phone=donor_phone,
                payment_method=payment_method or ("OFFLINE" if p.provider in ("OFFLINE", "BANK_DIRECT") else "ONLINE"),
                purpose=purpose,
                notes=notes,
                paid_at=p.paid_at,
                created_at=p.created_at,
            )
            items.append(item.model_dump(mode="json"))

        pagination = {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
        }
        return items, pagination

    async def record_manual_contribution(
        self,
        *,
        amount: Decimal,
        donor_name: str,
        donor_phone: str | None = None,
        group_id: uuid.UUID | None = None,
        member_id: uuid.UUID | None = None,
        payment_method: str = "CASH",
        transaction_id: str | None = None,
        purpose: str | None = None,
        notes: str | None = None,
        paid_at: datetime | None = None,
        current_user: Any = None,
    ) -> dict[str, Any]:
        """Record an offline / direct voluntary contribution by Admin."""
        from app.core.constants import PaymentStatus, PaymentWorkflow
        from datetime import timezone

        if amount <= Decimal("0"):
            raise BadRequestException("Contribution amount must be greater than 0")

        group_name = None
        if group_id:
            grp = await self.groups.get_by_id(group_id)
            if grp:
                group_name = grp.name

        member_name = donor_name
        if member_id:
            m_res = await self.session.execute(select(Member).where(Member.id == member_id).limit(1))
            m = m_res.scalar_one_or_none()
            if m:
                member_name = f"{m.first_name} {m.last_name or ''}".strip()
                if not donor_phone and getattr(m, "contact_phone", None):
                    donor_phone = m.contact_phone

        pay_time = paid_at or datetime.now(timezone.utc)
        receipt_code = f"REC-{uuid.uuid4().hex[:8].upper()}"
        order_code = f"cnt_man_{uuid.uuid4().hex[:10]}"
        txn_ref = transaction_id or f"TXN-{uuid.uuid4().hex[:10].upper()}"

        meta = {
            "purpose": purpose or "General Contribution",
            "notes": notes,
            "donor_name": donor_name,
            "donor_phone": donor_phone,
            "payment_method": payment_method.upper(),
            "receipt_no": receipt_code,
            "recorded_by": str(getattr(current_user, "id", "admin")),
            "is_manual": True,
        }

        payment = await self.payments.create(
            member_id=member_id,
            group_id=group_id,
            obligation_id=None,
            fee_id=None,
            order_id=order_code,
            amount=amount,
            currency="INR",
            status=PaymentStatus.SUCCESS.value,
            provider="OFFLINE" if payment_method.upper() in ("CASH", "CHEQUE") else "BANK_DIRECT",
            provider_reference=txn_ref,
            paid_at=pay_time,
            provider_metadata=json.dumps(meta),
            workflow=PaymentWorkflow.MEMBER_CONTRIBUTION.value,
        )

        try:
            from app.services.notification_service import create_payment_notification

            member_user_id = None
            if member_id:
                m_res = await self.session.execute(select(Member).where(Member.id == member_id).limit(1))
                m = m_res.scalar_one_or_none()
                if m:
                    member_user_id = m.user_id

            await create_payment_notification(
                self.session,
                category="contribution",
                amount=amount,
                donor_name=donor_name or member_name,
                group_name=group_name,
                purpose=purpose or "General Contribution",
                receipt_no=receipt_code,
                transaction_id=txn_ref,
                order_id=order_code,
                recipient_id=member_user_id,
                payment_id=payment.id,
                sent_at=pay_time,
                link="/admin/contributions",
            )
        except Exception as exc:
            logger.warning("Failed to create manual contribution notification: %s", exc)

        return {
            "id": str(payment.id),
            "order_id": payment.order_id,
            "transaction_id": txn_ref,
            "receipt_no": receipt_code,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
            "group_id": str(group_id) if group_id else None,
            "group_name": group_name,
            "member_id": str(member_id) if member_id else None,
            "member_name": member_name,
            "donor_name": donor_name,
            "donor_phone": donor_phone,
            "payment_method": payment_method.upper(),
            "purpose": purpose or "General Contribution",
            "notes": notes,
            "paid_at": pay_time.isoformat(),
            "created_at": payment.created_at.isoformat() if hasattr(payment, "created_at") else pay_time.isoformat(),
        }

