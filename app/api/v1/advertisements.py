from __future__ import annotations

import calendar
import json
import logging
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, or_, select

from app.core.constants import AdvertisementStatus, AuditAction
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import assert_group_access, resolve_managed_group
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.advertisement import Advertisement
from app.repositories.advertisement_repository import AdvertisementRepository
from app.schemas.advertisement import (
    AdvertisementApproveRequest,
    AdvertisementCreate,
    AdvertisementManualPaymentRequest,
    AdvertisementOut,
    AdvertisementPaymentOrderResponse,
    AdvertisementPaymentVerifyRequest,
    AdvertisementRenewRequest,
    AdvertisementUpdate,
    SubscriptionDurationOption,
    SubscriptionPricingUpdate,
)
from app.services.advertisement_service import AdvertisementService
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/advertisements", tags=["Advertisements"])

CREATE = Depends(require_permission(Permission.ADVERTISEMENT_CREATE))
APPROVE = Depends(require_permission(Permission.ADVERTISEMENT_APPROVE))
PRICING_MANAGE = Depends(
    require_permission(Permission.ADVERTISEMENT_PRICING_MANAGE)
)


def _now() -> datetime:
    return datetime.now(UTC)


def _to_utc_datetime(val: datetime | date | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC)
        return val
    if isinstance(val, date):
        return datetime.combine(val, time.min, tzinfo=UTC)
    return None


def _add_months(value: datetime, months: int) -> datetime:
    """Calendar-accurate expiry: 31 Jan + 1 month is the last day of February."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


async def _resolve_subscription(
    db: DBSession,
    *,
    months: int,
    start_dt: datetime,
    requested_amount: Decimal | float | None,
) -> tuple[object, datetime]:
    """Resolve the plan and validate the amount server-side.

    The price always comes from the active plan row. A submitted amount is only
    accepted when it matches that price, which catches a modal that went stale
    after a Super Admin changed pricing without trusting the client.
    """
    service = AdvertisementService(db)
    plan = await service.get_subscription_plan(months)
    if requested_amount is not None:
        requested = Decimal(str(requested_amount)).quantize(Decimal("0.01"))
        expected = Decimal(str(plan.price)).quantize(Decimal("0.01"))
        if requested != expected:
            raise BadRequestException(
                "Subscription price has changed. Refresh the plans and try again.",
                "SUBSCRIPTION_PRICE_CHANGED",
            )
    return plan, _add_months(start_dt, plan.months)


def _plan_out(plan) -> SubscriptionDurationOption:
    return SubscriptionDurationOption(
        months=plan.months,
        label=plan.label,
        description=plan.description
        or f"{plan.months} month advertisement subscription",
        price=plan.price,
        currency=plan.currency,
        is_default=plan.is_default,
    )


@router.get("/subscription-durations")
async def get_subscription_durations(db: DBSession):
    """Return active, database-backed advertisement subscription plans."""
    plans = await AdvertisementService(db).list_subscription_plans()
    return ok(
        "Subscription duration options fetched successfully",
        [_plan_out(plan) for plan in plans],
    )


@router.put("/subscription-durations", dependencies=[PRICING_MANAGE])
async def update_subscription_durations(
    body: SubscriptionPricingUpdate,
    current_user: CurrentUser,
    db: DBSession,
):
    """Update plan prices. Restricted to Super Admin by permission."""
    service = AdvertisementService(db)
    plans = await service.update_subscription_prices(
        [(item.months, Decimal(str(item.price))) for item in body.plans]
    )
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_PRICING_UPDATE,
        user_id=current_user.id,
        entity_type="advertisement_subscription_plan",
        details={
            "prices": {str(plan.months): str(plan.price) for plan in plans},
        },
    )
    await db.flush()
    return ok(
        "Advertisement subscription prices updated successfully",
        [_plan_out(plan) for plan in plans],
    )


@router.get("")
async def list_advertisements(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    placement: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List advertisements with optional filtering by group, placement, and status.

    Advertisements are home screen promotional banners visible to Super Admin,
    Group Admin, and Member alike. When group_id is not specified, all
    advertisements are returned regardless of the user's role or group.
    """
    stmt = select(Advertisement).where(Advertisement.is_deleted.is_(False))

    if group_id is not None:
        stmt = stmt.where(
            or_(
                Advertisement.group_id == group_id,
                Advertisement.group_id.is_(None),
            )
        )
    if active_only:
        stmt = stmt.where(
            Advertisement.is_active.is_(True),
            Advertisement.status == AdvertisementStatus.APPROVED.value,
        )
    if status is not None:
        stmt = stmt.where(Advertisement.status == status)
    if placement is not None:
        stmt = stmt.where(Advertisement.placement == placement)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    result = await db.execute(
        stmt.order_by(Advertisement.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Advertisements fetched successfully",
        [AdvertisementOut.model_validate(a) for a in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_advertisement(
    body: AdvertisementCreate, current_user: CurrentUser, db: DBSession
):
    """Create a new advertisement.

    Fields from creation form:
    - title (Advertise Title *)
    - group_id (Post By *)
    - start_date (Start Date *)
    - person_name (Person Name *)
    - mobile_number (Mobile Number *)
    - cover_image_url (Cover Image *)
    - large_image_url (Large Image *)
    - amount (optional; must match the active plan price if supplied)
    - subscription_months (Subscription duration dropdown)

    The advertisement end_date is independent from the subscription expiry. It
    is only stored when explicitly supplied; it is never derived from the plan.
    """
    effective_group_id = await resolve_managed_group(db, current_user, body.group_id)

    start_dt = _to_utc_datetime(body.start_date) or _now()
    months = (
        body.subscription_months
        if body.subscription_months and body.subscription_months > 0
        else 1
    )
    plan, expiry_dt = await _resolve_subscription(
        db,
        months=months,
        start_dt=start_dt,
        requested_amount=body.amount,
    )
    ad_end_dt = _to_utc_datetime(body.end_date)

    person_name = body.person_name or current_user.full_name or "Advertiser"
    mobile_number = body.mobile_number or current_user.phone_number or ""

    media_url = body.media_url or body.cover_image_url or body.large_image_url

    # Payment is currently optional. When created without payment,
    # it defaults to APPROVED and is_active=True.
    initial_status = body.status or AdvertisementStatus.APPROVED.value
    initial_payment_status = body.payment_status or "WAIVED"

    ad = await AdvertisementRepository(db).create(
        advertiser_id=current_user.id,
        group_id=effective_group_id,
        title=body.title,
        person_name=person_name,
        mobile_number=mobile_number,
        description=body.description,
        media_url=media_url,
        media_type=body.media_type,
        cover_image_url=body.cover_image_url,
        large_image_url=body.large_image_url,
        amount=plan.price,
        subscription_months=plan.months,
        subscription_plan_id=plan.id,
        currency=plan.currency,
        purchased_at=_now(),
        subscription_start_date=start_dt,
        subscription_expiry_date=expiry_dt,
        placement=body.placement,
        start_date=start_dt,
        end_date=ad_end_dt,
        target_url=body.target_url,
        status=initial_status,
        is_active=body.is_active if body.is_active is not None else True,
        payment_status=initial_payment_status,
        payment_id=body.payment_id,
    )
    await db.flush()

    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_CREATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={
            "title": ad.title,
            "group_id": str(ad.group_id) if ad.group_id else None,
        },
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Advertisement created successfully", AdvertisementOut.model_validate(ad)
    )


@router.post("/form", dependencies=[CREATE])
async def create_advertisement_form(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    title: str = Form(...),
    person_name: str | None = Form(default=None),
    mobile_number: str | None = Form(default=None),
    group_id: UUID | None = Form(default=None),
    start_date: str | None = Form(default=None),
    end_date: str | None = Form(default=None),
    subscription_months: int = Form(default=1),
    amount: Decimal | None = Form(default=None),
    description: str | None = Form(default=None),
    target_url: str | None = Form(default=None),
    placement: str = Form(default="HOME_BANNER"),
    cover_image: UploadFile | None = File(default=None),
    large_image: UploadFile | None = File(default=None),
):
    """Create an advertisement via multipart form, with optional images."""
    effective_group_id = await resolve_managed_group(db, current_user, group_id)

    parsed_start: datetime | None = None
    if start_date:
        try:
            parsed_start = datetime.fromisoformat(start_date)
        except ValueError:
            parsed_start = _now()
    start_dt = _to_utc_datetime(parsed_start) or _now()

    months = subscription_months if subscription_months > 0 else 1
    plan, expiry_dt = await _resolve_subscription(
        db,
        months=months,
        start_dt=start_dt,
        requested_amount=amount,
    )
    ad_end_dt: datetime | None = None
    if end_date:
        try:
            ad_end_dt = _to_utc_datetime(datetime.fromisoformat(end_date))
        except ValueError:
            ad_end_dt = None

    p_name = person_name or current_user.full_name or "Advertiser"
    m_number = mobile_number or current_user.phone_number or ""

    ad = await AdvertisementRepository(db).create(
        advertiser_id=current_user.id,
        group_id=effective_group_id,
        title=title,
        person_name=p_name,
        mobile_number=m_number,
        description=description,
        placement=placement,
        start_date=start_dt,
        end_date=ad_end_dt,
        target_url=target_url,
        amount=plan.price,
        subscription_months=plan.months,
        subscription_plan_id=plan.id,
        currency=plan.currency,
        purchased_at=_now(),
        subscription_start_date=start_dt,
        subscription_expiry_date=expiry_dt,
        status=AdvertisementStatus.APPROVED.value,
        is_active=True,
        payment_status="WAIVED",
    )
    await db.flush()

    ad_service = AdvertisementService(db)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    base_url = str(request.base_url)

    if cover_image and cover_image.filename:
        cover_bytes = await cover_image.read()
        if cover_bytes:
            await ad_service.upload_cover(
                ad,
                file_name=cover_image.filename,
                content=cover_bytes,
                mime_type=cover_image.content_type,
                uploaded_by=current_user.id,
                ip_address=ip,
                user_agent=ua,
                base_url=base_url,
            )

    if large_image and large_image.filename:
        large_bytes = await large_image.read()
        if large_bytes:
            await ad_service.upload_large(
                ad,
                file_name=large_image.filename,
                content=large_bytes,
                mime_type=large_image.content_type,
                uploaded_by=current_user.id,
                ip_address=ip,
                user_agent=ua,
                base_url=base_url,
            )

    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_CREATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={
            "title": ad.title,
            "group_id": str(ad.group_id) if ad.group_id else None,
        },
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Advertisement created successfully", AdvertisementOut.model_validate(ad)
    )


@router.get("/{advertisement_id}")
async def get_advertisement(advertisement_id: UUID, db: DBSession):
    ad = await AdvertisementRepository(db).get_by_id(advertisement_id)
    if ad is None or ad.is_deleted:
        raise NotFoundException("Advertisement", str(advertisement_id))
    return ok("Advertisement fetched successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/cover-image", dependencies=[CREATE])
async def upload_advertisement_cover_image(
    advertisement_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
):
    """Upload or replace advertisement cover image (Recommended: 1200x628, Max: 2MB)."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    content = await file.read()
    service = AdvertisementService(db)
    url = await service.upload_cover(
        ad,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={"field": "cover_image", "url": url},
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Cover image uploaded successfully", AdvertisementOut.model_validate(ad)
    )


@router.delete("/{advertisement_id}/cover-image", dependencies=[CREATE])
async def delete_advertisement_cover_image(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    """Delete advertisement cover image."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).remove_cover(ad)
    await db.flush()
    await db.refresh(ad)
    return ok("Cover image deleted successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/large-image", dependencies=[CREATE])
async def upload_advertisement_large_image(
    advertisement_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
):
    """Upload or replace advertisement large image (Max: 5MB)."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    content = await file.read()
    service = AdvertisementService(db)
    url = await service.upload_large(
        ad,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={"field": "large_image", "url": url},
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Large image uploaded successfully", AdvertisementOut.model_validate(ad)
    )


@router.delete("/{advertisement_id}/large-image", dependencies=[CREATE])
async def delete_advertisement_large_image(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    """Delete advertisement large image."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).remove_large(ad)
    await db.flush()
    await db.refresh(ad)
    return ok("Large image deleted successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/approve", dependencies=[APPROVE])
async def approve_advertisement(
    advertisement_id: UUID,
    body: AdvertisementApproveRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    if body.approve:
        ad.status = AdvertisementStatus.APPROVED.value
        ad.approved_by = current_user.id
        ad.approved_at = _now()
        ad.rejection_reason = None
    else:
        ad.status = AdvertisementStatus.REJECTED.value
        ad.rejection_reason = body.rejection_reason or "Rejected by admin"
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_APPROVE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    await db.refresh(ad)
    return ok(
        "Advertisement status updated successfully", AdvertisementOut.model_validate(ad)
    )


@router.patch("/{advertisement_id}", dependencies=[CREATE])
async def update_advertisement(
    advertisement_id: UUID,
    body: AdvertisementUpdate,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    dump = body.model_dump(exclude_unset=True)
    if "start_date" in dump:
        dump["start_date"] = _to_utc_datetime(dump["start_date"])
    if "end_date" in dump:
        dump["end_date"] = _to_utc_datetime(dump["end_date"])
    if "amount" in dump and dump["amount"] is not None:
        dump["amount"] = Decimal(str(dump["amount"])).quantize(Decimal("0.01"))
    if "subscription_months" in dump and dump["subscription_months"] is not None:
        dump["subscription_months"] = int(dump["subscription_months"])

    ad = await repo.update(ad, **dump)
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_UPDATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    await db.refresh(ad)
    return ok("Advertisement updated successfully", AdvertisementOut.model_validate(ad))


@router.delete("/{advertisement_id}", dependencies=[CREATE])
async def delete_advertisement(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).delete_all_for_ad(ad)
    await repo.delete(ad)
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_DELETE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    return ok("Advertisement deleted successfully")


# ── Advertisement Renewal & Payment Endpoints ─────────────────────────


@router.post("/{advertisement_id}/renew", dependencies=[CREATE])
async def renew_advertisement(
    advertisement_id: UUID,
    body: AdvertisementRenewRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    """Renew an advertisement subscription with a new period and auto-calculated end date."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    plan = await AdvertisementService(db).resolve_plan(body.subscription_months)
    now_dt = _now()

    # If the ad has a future end_date, start renewal from that date; otherwise from now
    start_dt = _to_utc_datetime(body.start_date)
    if not start_dt:
        if ad.end_date and ad.end_date > now_dt:
            start_dt = ad.end_date
        else:
            start_dt = now_dt

    # End date is auto-calculated from subscription months
    end_dt = _to_utc_datetime(body.end_date)
    if not end_dt:
        end_dt = _add_months(start_dt, plan.months)

    ad.subscription_months = plan.months
    ad.subscription_plan_id = plan.id
    ad.amount = plan.price
    ad.currency = plan.currency
    ad.start_date = start_dt
    ad.end_date = end_dt
    ad.subscription_start_date = start_dt
    ad.subscription_expiry_date = end_dt
    ad.status = AdvertisementStatus.APPROVED.value
    ad.is_active = True
    ad.payment_status = body.payment_status or "PAID"
    ad.rejection_reason = None
    ad.purchased_at = now_dt

    await db.flush()
    await db.refresh(ad)

    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_UPDATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={"action": "renew", "months": plan.months, "amount": float(plan.price)},
    )
    return ok("Advertisement renewed successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/payment/order", dependencies=[CREATE])
async def create_ad_payment_order(
    advertisement_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
):
    """Create a Razorpay order for advertisement subscription payment."""
    from app.core.config import get_settings
    from app.integrations.razorpay import create_razorpay_order
    from app.models.payment import Payment

    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))

    amount = Decimal(str(ad.amount or 0))
    if amount <= Decimal("0"):
        ad.payment_status = "WAIVED"
        await db.flush()
        return ok("Advertisement payment waived", {"status": "WAIVED"})

    settings = get_settings()
    receipt = f"ad_{str(ad.id)[:12]}"
    notes = {
        "workflow": "ADVERTISEMENT_SUBSCRIPTION",
        "advertisement_id": str(ad.id),
        "title": ad.title[:50],
        "user_id": str(current_user.id),
    }

    rzp_order = create_razorpay_order(
        amount=amount,
        currency="INR",
        receipt=receipt,
        notes=notes,
    )

    payment = Payment(
        member_id=None,
        group_id=ad.group_id,
        obligation_id=None,
        fee_id=None,
        order_id=rzp_order["id"],
        amount=amount,
        currency="INR",
        status="PENDING",
        provider="RAZORPAY",
        provider_reference=None,
        paid_at=None,
        provider_metadata=json.dumps({"advertisement_id": str(ad.id), "title": ad.title}),
        workflow="ADVERTISEMENT_SUBSCRIPTION",
    )
    db.add(payment)
    await db.flush()

    ad.payment_id = payment.id
    ad.payment_status = "PENDING"
    await db.flush()

    return ok(
        "Payment order created successfully",
        AdvertisementPaymentOrderResponse(
            order_id=str(payment.id),
            razorpay_order_id=rzp_order["id"],
            razorpay_key_id=settings.razorpay_key_id,
            amount=float(amount),
            currency="INR",
            advertisement_id=ad.id,
            title=ad.title,
        ).model_dump(),
    )


@router.post("/{advertisement_id}/payment/verify", dependencies=[CREATE])
async def verify_ad_payment(
    advertisement_id: UUID,
    body: AdvertisementPaymentVerifyRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    """Verify Razorpay payment signature and mark advertisement as PAID and APPROVED."""
    from app.integrations.razorpay import verify_razorpay_payment_signature
    from app.models.payment import Payment

    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))

    verify_razorpay_payment_signature(
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature,
    )

    if ad.payment_id:
        p_res = await db.execute(select(Payment).where(Payment.id == ad.payment_id))
        payment = p_res.scalar_one_or_none()
        if payment:
            payment.status = "SUCCESS"
            payment.provider_reference = body.razorpay_payment_id
            payment.paid_at = _now()

    ad.payment_status = "PAID"
    ad.status = AdvertisementStatus.APPROVED.value
    ad.is_active = True

    try:
        from decimal import Decimal
        from app.services.notification_service import create_payment_notification

        await create_payment_notification(
            db,
            category="ad_payment",
            amount=Decimal(str(ad.amount or 0)),
            group_id=getattr(ad, "group_id", None),
            purpose=ad.title,
            receipt_no=body.razorpay_payment_id,
            transaction_id=body.razorpay_payment_id,
            order_id=body.razorpay_order_id,
            recipient_id=current_user.id if current_user else None,
            payment_id=ad.id,
            link="/admin/advertisements",
        )
    except Exception as exc:
        logger.warning("Failed to create ad payment notification: %s", exc)

    await db.flush()
    await db.refresh(ad)

    return ok("Payment verified and advertisement activated", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/payment/link", dependencies=[CREATE])
async def create_ad_payment_link(
    advertisement_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
):
    """Generate a mobile-friendly Razorpay payment link for the advertiser."""
    from app.integrations.razorpay import create_razorpay_payment_link

    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))

    amount = Decimal(str(ad.amount or 0))
    if amount <= Decimal("0"):
        raise BadRequestException("Cannot generate payment link for 0 amount")

    customer_phone = (ad.mobile_number or "").replace(" ", "").replace("-", "")
    customer_name = ad.person_name or "Advertiser"

    link_data = create_razorpay_payment_link(
        amount=amount,
        description=f"JSSG Ad: {ad.title[:40]} ({ad.subscription_months} mo)",
        customer_name=customer_name,
        customer_contact=customer_phone if len(customer_phone) >= 10 else None,
        notes={
            "workflow": "ADVERTISEMENT_SUBSCRIPTION",
            "advertisement_id": str(ad.id),
            "title": ad.title[:50],
        },
    )

    return ok(
        "Payment link created successfully",
        {
            "payment_link_id": link_data.get("id"),
            "short_url": link_data.get("short_url"),
            "amount": float(amount),
            "currency": "INR",
        },
    )


@router.post("/{advertisement_id}/payment/manual", dependencies=[CREATE])
async def record_ad_manual_payment(
    advertisement_id: UUID,
    body: AdvertisementManualPaymentRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    """Record manual/offline payment (Cash/Cheque/Bank Transfer) or Waive ad fee."""
    from app.models.payment import Payment

    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))

    method = (body.payment_method or "CASH").upper()
    now_dt = _now()

    if method == "WAIVED":
        ad.payment_status = "WAIVED"
        ad.status = AdvertisementStatus.APPROVED.value
        ad.is_active = True
        await db.flush()
        await db.refresh(ad)
        return ok("Advertisement payment waived", AdvertisementOut.model_validate(ad))

    amount = Decimal(str(ad.amount or 0))
    txn_ref = body.transaction_id or f"OFF-{now_dt.strftime('%Y%m%d')}-{str(ad.id)[:6].upper()}"

    meta = {
        "payment_method": method,
        "notes": body.notes,
        "advertisement_id": str(ad.id),
        "recorded_by": str(current_user.id),
    }

    payment = Payment(
        member_id=None,
        group_id=ad.group_id,
        obligation_id=None,
        fee_id=None,
        order_id=f"ad_man_{str(uuid.uuid4())[:8]}",
        amount=amount,
        currency="INR",
        status="SUCCESS",
        provider="OFFLINE" if method in ("CASH", "CHEQUE") else "BANK_DIRECT",
        provider_reference=txn_ref,
        paid_at=now_dt,
        provider_metadata=json.dumps(meta),
        workflow="ADVERTISEMENT_SUBSCRIPTION",
    )
    db.add(payment)
    await db.flush()

    ad.payment_id = payment.id
    ad.payment_status = "PAID"
    ad.status = AdvertisementStatus.APPROVED.value
    ad.is_active = True

    try:
        from decimal import Decimal
        from app.services.notification_service import create_payment_notification

        await create_payment_notification(
            db,
            category="ad_payment",
            amount=Decimal(str(ad.amount or 0)),
            group_id=getattr(ad, "group_id", None),
            purpose=ad.title,
            receipt_no=txn_ref,
            transaction_id=txn_ref,
            order_id=str(payment.id),
            recipient_id=current_user.id if current_user else None,
            payment_id=ad.id,
            link="/admin/advertisements",
        )
    except Exception as exc:
        logger.warning("Failed to create ad payment notification: %s", exc)

    await db.flush()
    await db.refresh(ad)

    return ok("Manual payment recorded successfully", AdvertisementOut.model_validate(ad))

