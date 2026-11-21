from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.member import Member
from app.models.payment import Payment
from app.repositories.group_repository import GroupRepository
from app.repositories.payment_repository import PaymentRepository
from app.schemas.group_payment import (
    GroupCheckoutOrderCreateRequest,
    GroupObligationOut,
    GroupPaymentDetailOut,
    GroupPaymentLinkResponse,
    GroupPaymentOrderCreateResponse,
    GroupPaymentVerifyRequest,
    PaymentSettingsModel,
    PaymentSummaryOut,
)
from app.schemas.payment import (
    ContributionHistoryItem,
    ContributionManualRequest,
    ContributionOrderRequest,
    ContributionOrderResponse,
    ContributionVerifyRequest,
    ContributionVerifyResponse,
    PaymentCreate,
    PaymentCreateResponse,
    PaymentOut,
    PaymentVerifyRequest,
)
from app.schemas.group_fee import GroupFeeOrderRequest, GroupFeeVerifyRequest
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/payments", tags=["Payments"])

READ = Depends(require_permission(Permission.PAYMENT_READ))
MANAGE = Depends(require_permission(Permission.PAYMENT_MANAGE))


@router.get("/summary", dependencies=[READ])
async def get_payments_summary(
    db: DBSession,
    billing_period: str | None = Query(default=None),
):
    """Global dashboard payment totals: expected, collected, outstanding, transactions."""
    service = PaymentService(db)
    summary = await service.get_payment_summary(billing_period=billing_period)
    return ok("Payment summary fetched successfully", summary.model_dump())


@router.get("/groups", dependencies=[READ])
async def list_group_payment_obligations(
    db: DBSession,
    billing_period: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Group-wise payment status list with collection progress and active payment links."""
    service = PaymentService(db)
    items, pagination = await service.list_group_obligations(
        billing_period=billing_period,
        search=search,
        page=page,
        page_size=page_size,
    )
    return paginated(
        "Group obligations fetched successfully",
        [item.model_dump(by_alias=True) for item in items],
        pagination,
    )


@router.get("/groups/{group_id}", dependencies=[READ])
async def get_group_payment_obligation(
    group_id: UUID,
    db: DBSession,
    billing_period: str | None = Query(default=None),
):
    """Detailed group payment obligation summary and complete transaction history."""
    service = PaymentService(db)
    detail = await service.get_group_obligation_detail(
        group_id=group_id, billing_period=billing_period
    )
    return ok("Group obligation detail fetched successfully", detail.model_dump(by_alias=True))


@router.get("/settings", dependencies=[READ])
async def get_payment_settings(
    db: DBSession,
):
    """Fetch global payment collection module configuration."""
    service = PaymentService(db)
    settings_data = await service.get_payment_settings()
    return ok("Payment settings fetched successfully", settings_data)


@router.put("/settings", dependencies=[MANAGE])
@router.patch("/settings", dependencies=[MANAGE])
async def update_payment_settings(
    request: Request,
    db: DBSession,
):
    """Update payment collection settings and optionally apply to existing obligations."""
    import json
    raw = await request.body()
    data: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except Exception:
                    pass
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass
    validated = PaymentSettingsModel.model_validate(data)
    service = PaymentService(db)
    updated = await service.update_payment_settings(validated.model_dump())
    return ok("Payment settings updated successfully", updated)


@router.post("/groups/{group_id}/order", dependencies=[MANAGE])
async def create_group_checkout_order(
    group_id: UUID,
    db: DBSession,
    request: Request,
    body: GroupCheckoutOrderCreateRequest | None = None,
    billing_period: str | None = Query(default=None),
    amount: float | None = Query(default=None),
):
    """WORKFLOW A: Create a Razorpay Order for Super Admin checkout."""
    service = PaymentService(db)
    chosen_period = (body.billing_period if body and body.billing_period else None) or billing_period
    chosen_amount = (body.amount if body and body.amount is not None else None) or amount
    order_res = await service.create_group_checkout_order(
        group_id=group_id,
        billing_period=chosen_period,
        amount=chosen_amount,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return created("Group checkout order created successfully", order_res.model_dump())


@router.post("/groups/{group_id}/verify", dependencies=[MANAGE])
async def verify_group_checkout_payment(
    group_id: UUID,
    body: GroupPaymentVerifyRequest,
    db: DBSession,
    request: Request,
):
    """WORKFLOW A: Verify Super Admin checkout signature and credit common ledger."""
    service = PaymentService(db)
    detail = await service.verify_group_checkout_payment(
        group_id=group_id,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok("Payment verified and ledger updated successfully", detail.model_dump(by_alias=True))


@router.post("/groups/{group_id}/link", dependencies=[MANAGE])
async def create_group_payment_link(
    group_id: UUID,
    db: DBSession,
    request: Request,
    billing_period: str | None = Query(default=None),
):
    """WORKFLOW B: Generate mobile-friendly Razorpay Payment Link for Group Admin."""
    service = PaymentService(db)
    link_res = await service.create_group_payment_link(
        group_id=group_id,
        billing_period=billing_period,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return created("Group payment link created successfully", link_res.model_dump())


async def _serialize_payments_with_group_info(
    db, payments: list[Payment]
) -> list[PaymentOut]:
    if not payments:
        return []

    # Collect group_ids to batch query member counts if not in snapshot
    group_ids: set[UUID] = set()
    for p in payments:
        gid = p.group_id or (p.obligation.group_id if p.obligation else None) or (p.member.group_id if p.member else None)
        if gid:
            group_ids.add(gid)

    counts_map = await GroupRepository(db).get_member_counts(list(group_ids)) if group_ids else {}

    serialized: list[PaymentOut] = []
    for p in payments:
        gid = p.group_id or (p.obligation.group_id if p.obligation else None) or (p.member.group_id if p.member else None)
        gname: str | None = None
        if p.obligation and p.obligation.group_name:
            gname = p.obligation.group_name
        elif p.group and p.group.name:
            gname = p.group.name
        elif p.member and p.member.group and p.member.group.name:
            gname = p.member.group.name

        if not gname and p.provider_metadata:
            try:
                meta = json.loads(p.provider_metadata) if isinstance(p.provider_metadata, str) else p.provider_metadata
                donor = meta.get("donor_name")
                purpose = meta.get("purpose")
                if donor:
                    gname = f"{donor}"
                elif purpose:
                    gname = f"{purpose}"
            except Exception:
                pass

        total_members: int | None = None
        if p.obligation and p.obligation.member_count_snapshot is not None:
            total_members = p.obligation.member_count_snapshot
        elif gid:
            total_members = counts_map.get(gid)

        total_amount = None
        if p.obligation and p.obligation.expected_amount is not None:
            total_amount = p.obligation.expected_amount
        else:
            total_amount = p.amount

        out = PaymentOut.model_validate(p)
        out.group_id = gid
        out.group_name = gname
        out.total_members = total_members
        out.total_amount = total_amount
        serialized.append(out)

    return serialized


@router.get("", dependencies=[READ])
async def list_payments(
    db: DBSession,
    member_id: UUID | None = Query(default=None),
    group_only: bool = Query(default=False),
    workflow: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    stmt = (
        select(Payment)
        .options(
            selectinload(Payment.group),
            selectinload(Payment.obligation),
            selectinload(Payment.member).selectinload(Member.group),
        )
    )
    if member_id:
        stmt = stmt.where(Payment.member_id == member_id)
    if group_only:
        stmt = stmt.where(
            or_(
                Payment.obligation_id.isnot(None),
                Payment.workflow.in_(["SUPER_ADMIN_CHECKOUT", "GROUP_ADMIN_PAYMENT_LINK"]),
            )
        )
    elif workflow:
        stmt = stmt.where(Payment.workflow == workflow)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Payment.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    serialized = await _serialize_payments_with_group_info(db, items)
    if member_id:
        return ok("Payments fetched successfully", serialized)
    return paginated(
        "Payments fetched successfully",
        serialized,
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("/create-order", dependencies=[MANAGE])
async def create_group_fee_order(
    body: GroupFeeOrderRequest,
    current_user: CurrentUser,
    db: DBSession,
    request: Request,
):
    """Create a Razorpay order for Group Fee payment (Flutter app flow).

    Security:
    - Only verified Group Admins of the specified group (or Super Admin) can initiate.
    - Amount is calculated purely server-side (active members * rate).
    - Returns order ID and public key (key secret is never exposed).
    """
    service = PaymentService(db)
    result = await service.create_group_fee_order(
        group_id=body.group_id,
        fee_id=body.fee_id,
        user=current_user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return created("Payment order created successfully", result)


@router.post("/contributions/order", status_code=201)
async def create_contribution_order(
    body: ContributionOrderRequest,
    current_user: CurrentUser,
    db: DBSession,
    request: Request,
):
    """Create a Razorpay order for voluntary contribution/donation.

    Open to all authenticated users (members, group admins, super admins).
    Accepts any custom amount. No group obligation lock.
    """
    service = PaymentService(db)
    result = await service.create_contribution_order(
        amount=body.amount,
        user=current_user,
        group_id=body.group_id,
        event_id=body.event_id,
        purpose=body.purpose,
        notes=body.notes,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return created("Contribution order created successfully", result)


@router.post("/contributions/verify")
async def verify_contribution_payment(
    body: ContributionVerifyRequest,
    current_user: CurrentUser,
    db: DBSession,
    request: Request,
):
    """Verify Razorpay payment signature for voluntary contribution."""
    service = PaymentService(db)
    result = await service.verify_contribution_payment(
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature,
        payment_id=body.payment_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok("Contribution payment verified successfully", result)


@router.get("/contributions")
async def list_contributions(
    current_user: CurrentUser,
    db: DBSession,
    group_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List successful voluntary contributions."""
    service = PaymentService(db)
    items, pagination = await service.list_contributions(
        user=current_user,
        group_id=group_id,
        page=page,
        page_size=page_size,
    )
    return paginated("Contributions fetched successfully", items, pagination)


@router.post("/contributions/manual", status_code=201)
async def record_manual_contribution(
    body: ContributionManualRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    """Record an offline / direct voluntary contribution (Super Admin / Group Admin)."""
    service = PaymentService(db)
    result = await service.record_manual_contribution(
        amount=body.amount,
        donor_name=body.donor_name,
        donor_phone=body.donor_phone,
        group_id=body.group_id,
        member_id=body.member_id,
        payment_method=body.payment_method,
        transaction_id=body.transaction_id,
        purpose=body.purpose,
        notes=body.notes,
        paid_at=body.paid_at,
        current_user=current_user,
    )
    return created("Contribution recorded successfully", result)


@router.post("")
async def create_payment(body: PaymentCreate, current_user: CurrentUser, db: DBSession):
    """Create a Razorpay order and return the details needed by Flutter Checkout."""
    service = PaymentService(db)
    payment = await service.create_order(
        member_id=body.member_id,
        fee_id=body.fee_id,
        amount=body.amount,
        currency=body.currency,
    )
    return created(
        "Payment order created successfully",
        PaymentCreateResponse(
            id=payment.id,
            order_id=payment.order_id,
            razorpay_order_id=payment.order_id,
            razorpay_key_id=settings.razorpay_key_id,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status,
            provider=payment.provider,
            created_at=payment.created_at,
        ).model_dump(mode="json"),
    )


@router.get("/{payment_id}", dependencies=[READ])
async def get_payment(payment_id: UUID, db: DBSession):
    stmt = (
        select(Payment)
        .where(Payment.id == payment_id)
        .options(
            selectinload(Payment.group),
            selectinload(Payment.obligation),
            selectinload(Payment.member).selectinload(Member.group),
        )
    )
    res = await db.execute(stmt)
    payment = res.scalar_one_or_none()
    if payment is None:
        raise NotFoundException("Payment", str(payment_id))
    serialized = await _serialize_payments_with_group_info(db, [payment])
    return ok("Payment fetched successfully", serialized[0])


@router.post("/verify")
async def verify_payment(
    body: PaymentVerifyRequest,
    db: DBSession,
    current_user: CurrentUser,
    request: Request,
):
    """Verify Razorpay payment signature after Flutter Checkout completes.

    The Flutter app sends ``razorpay_order_id``, ``razorpay_payment_id``,
    and ``razorpay_signature`` here. We verify the HMAC signature and update
    the payment status accordingly.
    """
    service = PaymentService(db)
    payment = await service.payments.get_by_order_id(body.razorpay_order_id)
    if payment is None and body.payment_id:
        payment = await service.payments.get_by_id(body.payment_id)

    if payment and payment.obligation_id:
        # Group Fee payment flow
        result = await service.verify_group_fee_payment(
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
            payment_id=body.payment_id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return ok("Payment verified successfully", result)

    # Standard/legacy payment flow
    verified_payment = await service.verify_payment(
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(
        "Payment verified successfully",
        {
            "status": verified_payment.status,
            "message": "Payment verified successfully",
            "transaction_id": verified_payment.provider_reference or body.razorpay_payment_id,
            "paid_at": verified_payment.paid_at,
            "receipt_url": None,
        },
    )


@router.post("/webhook")
async def razorpay_webhook(request: Request, db: DBSession):
    """Receive Razorpay webhook events.

    This endpoint is called directly by Razorpay servers — it requires
    **no authentication** but verifies the ``X-Razorpay-Signature`` header
    using our webhook secret.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    service = PaymentService(db)
    result = await service.handle_webhook(body=body, signature=signature)

    logger.info("Webhook processed: %s", result)
    return ok("Webhook processed successfully", result)


@router.post("/{payment_id}/verify", dependencies=[MANAGE])
async def verify_payment_manual(
    payment_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Manual verification by admin (mark as SUCCESS)."""
    from app.core.constants import PaymentStatus

    repo = PaymentRepository(db)
    payment = await repo.get_by_id(payment_id)
    if payment is None:
        raise NotFoundException("Payment", str(payment_id))
    payment = await PaymentService(db).verify(
        payment,
        provider_reference=f"MANUAL-{current_user.id}",
        status=PaymentStatus.SUCCESS,
    )
    return ok("Payment verified successfully", PaymentOut.model_validate(payment))
