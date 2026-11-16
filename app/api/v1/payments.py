from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.config import get_settings
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.payment import Payment
from app.repositories.payment_repository import PaymentRepository
from app.schemas.group_payment import (
    GroupObligationOut,
    GroupPaymentDetailOut,
    GroupPaymentLinkResponse,
    GroupPaymentOrderCreateResponse,
    GroupPaymentVerifyRequest,
    PaymentSummaryOut,
)
from app.schemas.payment import (
    ContributionHistoryItem,
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


@router.post("/groups/{group_id}/order", dependencies=[MANAGE])
async def create_group_checkout_order(
    group_id: UUID,
    db: DBSession,
    request: Request,
    billing_period: str | None = Query(default=None),
):
    """WORKFLOW A: Create a Razorpay Order for Super Admin checkout."""
    service = PaymentService(db)
    order_res = await service.create_group_checkout_order(
        group_id=group_id,
        billing_period=billing_period,
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


@router.get("", dependencies=[READ])
async def list_payments(
    db: DBSession,
    member_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = PaymentRepository(db)
    if member_id:
        items = await repo.list_for_member(
            member_id, limit=page_size, offset=(page - 1) * page_size
        )
        return ok(
            "Payments fetched successfully",
            [PaymentOut.model_validate(p) for p in items],
        )
    stmt = select(Payment)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Payment.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Payments fetched successfully",
        [PaymentOut.model_validate(p) for p in items],
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
    payment = await PaymentRepository(db).get_by_id(payment_id)
    if payment is None:
        raise NotFoundException("Payment", str(payment_id))
    return ok("Payment fetched successfully", PaymentOut.model_validate(payment))


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
