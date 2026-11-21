from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification

logger = logging.getLogger(__name__)


def format_currency_inr(amount: Decimal | float | int) -> str:
    try:
        amt = float(amount)
        if amt.is_integer():
            return f"₹{int(amt):,}"
        return f"₹{amt:,.2f}"
    except Exception:
        return f"₹{amount}"


async def create_payment_notification(
    session: AsyncSession,
    *,
    category: str,  # "contribution" | "group_payment" | "ad_payment" | "payment"
    amount: Decimal | float,
    donor_name: str | None = None,
    group_name: str | None = None,
    purpose: str | None = None,
    receipt_no: str | None = None,
    transaction_id: str | None = None,
    order_id: str | None = None,
    recipient_id: UUID | None = None,
    payment_id: UUID | None = None,
    sent_at: datetime | None = None,
    link: str | None = None,
) -> Notification | None:
    """Create a payment or contribution notification in the database."""
    try:
        amt_str = format_currency_inr(amount)
        amt_float = float(amount)

        if category == "contribution":
            title = "Contribution Received"
            purpose_str = f" ({purpose})" if purpose else ""
            donor_str = donor_name or "Anonymous"
            body = f"Contribution of {amt_str} received from {donor_str}{purpose_str}"
            notification_type = "CONTRIBUTION"
            default_link = "/admin/contributions"
        elif category == "group_payment":
            title = "Group Payment Received"
            grp_str = f" from {group_name}" if group_name else ""
            body = f"Payment of {amt_str} received{grp_str}"
            notification_type = "GROUP_PAYMENT"
            default_link = "/admin/payments"
        elif category == "ad_payment":
            title = "Ad Payment Received"
            ad_str = f": {purpose}" if purpose else ""
            body = f"Payment of {amt_str} received for advertisement{ad_str}"
            notification_type = "AD_PAYMENT"
            default_link = "/admin/advertisements"
        else:
            title = "Payment Received"
            grp_str = f" from {group_name or donor_name}" if (group_name or donor_name) else ""
            body = f"Payment of {amt_str} received{grp_str}"
            notification_type = "PAYMENT"
            default_link = "/admin/payments"

        meta = {
            "category": category,
            "amount": amt_float,
            "donor_name": donor_name,
            "group_name": group_name,
            "purpose": purpose,
            "receipt_no": receipt_no,
            "transaction_id": transaction_id,
            "order_id": order_id,
            "link": link or default_link,
        }

        notif = Notification(
            recipient_id=recipient_id,
            title=title,
            body=body,
            data=json.dumps(meta),
            notification_type=notification_type,
            entity_type="payment" if category != "contribution" else "contribution",
            entity_id=payment_id,
            sent_at=sent_at or datetime.now(UTC),
        )
        session.add(notif)
        await session.flush()
        return notif
    except Exception as exc:
        logger.warning("Could not create payment notification: %s", exc)
        return None
