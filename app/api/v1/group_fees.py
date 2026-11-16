"""Group-level fee status and payment history endpoints for the Flutter mobile app.

These endpoints are scoped under ``/groups/{group_id}/...`` and provide the exact
JSON payloads the Flutter Razorpay integration expects.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.permissions import Permission
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession, require_permission

router = APIRouter(prefix="/groups", tags=["Group Fee Payments"])

READ = Depends(require_permission(Permission.FEE_READ))


@router.get("/{group_id}/fee", dependencies=[READ])
async def get_group_fee_status(
    group_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    financial_year: str | None = Query(default=None),
):
    """Fetch group fee status & calculation for the Flutter app.

    Returns the fee breakdown (active_members × fee_per_member),
    current payment status, and whether the authenticated user is
    a verified admin of this group.

    The response contains all information the Flutter app needs to
    display the fee card and decide whether to show admin payment controls.
    """
    from app.services.payment_service import PaymentService

    service = PaymentService(db)
    data = await service.get_group_fee_status(
        group_id=group_id,
        user=current_user,
        billing_period=financial_year,
    )
    return ok("Group fee status fetched successfully", data)


@router.get("/{group_id}/payments", dependencies=[READ])
async def get_group_payment_history(
    group_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return list of past successful fee payments for a group.

    Includes financial year, date, amount, Razorpay transaction ID,
    and receipt URL for each payment.
    """
    from app.services.payment_service import PaymentService

    service = PaymentService(db)
    items = await service.get_group_payment_history(
        group_id=group_id,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return ok("Group payment history fetched successfully", items)
