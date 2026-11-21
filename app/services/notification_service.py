from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import DeviceToken, Notification

logger = logging.getLogger(__name__)


def format_currency_inr(amount: Decimal | float | int) -> str:
    try:
        amt = float(amount)
        if amt.is_integer():
            return f"₹{int(amt):,}"
        return f"₹{amt:,.2f}"
    except Exception:
        return f"₹{amount}"


async def get_active_device_tokens(session: AsyncSession, user_id: UUID) -> list[str]:
    """Fetch user's active device tokens from database."""
    try:
        result = await session.execute(
            select(DeviceToken.token).where(
                DeviceToken.user_id == user_id,
                DeviceToken.is_active.is_(True),
            )
        )
        return [t for t in result.scalars().all() if t]
    except Exception as exc:
        logger.warning("Failed to query active device tokens for user %s: %s", user_id, exc)
        return []


async def send_push_notification_to_user(
    session: AsyncSession,
    user_id: UUID,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Fetch active device tokens for user and send FCM push notifications.

    Returns the number of successfully delivered pushes.
    Catches all exceptions so calling flows are never interrupted.
    """
    try:
        from app.integrations.firebase import send_fcm_push_async

        stmt = select(DeviceToken).where(
            DeviceToken.user_id == user_id,
            DeviceToken.is_active.is_(True),
        )
        res = await session.execute(stmt)
        device_tokens = list(res.scalars().all())
        if not device_tokens:
            logger.info("No active device tokens found for user %s", user_id)
            return 0

        success_count = 0
        for dt in device_tokens:
            try:
                ok = await send_fcm_push_async(
                    device_token=dt.token,
                    title=title,
                    body=body,
                    data=data,
                )
                if ok:
                    success_count += 1
            except Exception as e:
                logger.warning("Failed to send push to token %s...: %s", dt.token[:15], e)

        logger.info(
            "FCM Push delivered to %d/%d devices for user %s",
            success_count,
            len(device_tokens),
            user_id,
        )
        return success_count
    except Exception as exc:
        logger.error("Failed to send push notification to user %s: %s", user_id, exc)
        return 0


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
    """Create a payment or contribution notification in the database and trigger FCM push."""
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

        # Trigger FCM push notification to recipient's active devices
        if recipient_id:
            try:
                push_title = title
                push_body = body
                if category == "contribution":
                    d_name = donor_name or "Anonymous"
                    try:
                        amt_num = float(amount)
                        amt_disp = int(amt_num) if amt_num.is_integer() else f"{amt_num:.2f}"
                        push_body = f"Contribution of ₹{amt_disp} received from {d_name}"
                    except Exception:
                        push_body = body

                push_payload = {
                    "category": category,
                    "amount": str(amount),
                    "donor_name": donor_name or "",
                    "group_name": group_name or "",
                    "receipt_no": receipt_no or "",
                    "order_id": order_id or "",
                    "notification_type": notification_type,
                    "notification_id": str(notif.id),
                }

                await send_push_notification_to_user(
                    session,
                    recipient_id,
                    title=push_title,
                    body=push_body,
                    data=push_payload,
                )
            except Exception as push_err:
                logger.warning("FCM push trigger failed (DB notification saved): %s", push_err)

        return notif
    except Exception as exc:
        logger.warning("Could not create payment notification: %s", exc)
        return None
