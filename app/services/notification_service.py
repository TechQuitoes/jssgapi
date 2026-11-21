from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
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


async def get_admin_user_ids(
    session: AsyncSession, group_id: UUID | None = None
) -> list[UUID]:
    """Fetch user IDs for all SUPER_ADMINs, plus GROUP_ADMINs of the given group."""
    try:
        from app.models.member_group import GroupMember
        from app.models.user import Role, User

        admin_ids: set[UUID] = set()

        # 1. Platform-wide Super Admins (SUPER_ADMIN, ADMIN, FEDERATION_ADMIN)
        stmt_super = (
            select(User.id)
            .join(Role, User.role_id == Role.id)
            .where(
                func.upper(func.trim(Role.name)).in_(
                    ["SUPER_ADMIN", "ADMIN", "FEDERATION_ADMIN"]
                ),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        res_super = await session.execute(stmt_super)
        for uid in res_super.scalars().all():
            if uid:
                admin_ids.add(uid)

        # 2. Group Admins of the specified group
        if group_id:
            stmt_ga = (
                select(User.id)
                .join(Role, User.role_id == Role.id)
                .where(
                    func.upper(func.trim(Role.name)).in_(
                        ["GROUP_ADMIN", "REGIONAL_ADMIN"]
                    ),
                    User.group_id == group_id,
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                )
            )
            res_ga = await session.execute(stmt_ga)
            for uid in res_ga.scalars().all():
                if uid:
                    admin_ids.add(uid)

            # Check GroupMember table for members with GROUP_ADMIN role
            stmt_gm = (
                select(GroupMember.user_id)
                .join(User, GroupMember.user_id == User.id)
                .where(
                    GroupMember.group_id == group_id,
                    func.upper(func.trim(GroupMember.role)) == "GROUP_ADMIN",
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                )
            )
            res_gm = await session.execute(stmt_gm)
            for uid in res_gm.scalars().all():
                if uid:
                    admin_ids.add(uid)

        return list(admin_ids)
    except Exception as exc:
        logger.error("Failed to query admin user IDs for group %s: %s", group_id, exc)
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


async def send_push_notification_to_users(
    session: AsyncSession,
    user_ids: list[UUID],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Send push notification to all active devices of the specified list of users via multicast."""
    if not user_ids:
        return 0
    try:
        from app.integrations.firebase import send_fcm_multicast_async

        stmt = select(DeviceToken.token).where(
            DeviceToken.user_id.in_(user_ids),
            DeviceToken.is_active.is_(True),
        )
        res = await session.execute(stmt)
        tokens = list(dict.fromkeys([t for t in res.scalars().all() if t and t.strip()]))
        if not tokens:
            logger.info("No active device tokens found for %d users", len(user_ids))
            return 0

        success = await send_fcm_multicast_async(tokens, title, body, data)
        logger.info(
            "Push notification delivered to %d/%d devices for %d users",
            success,
            len(tokens),
            len(user_ids),
        )
        return success
    except Exception as exc:
        logger.error("Failed to send push to users: %s", exc)
        return 0


async def broadcast_push_notification(
    session: AsyncSession,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    user_ids: list[UUID] | None = None,
) -> int:
    """Send FCM push notification to all active device tokens (across ALL roles: MEMBER, GROUP_ADMIN, SUPER_ADMIN)
    or a specific list of user IDs.
    """
    try:
        from app.integrations.firebase import send_fcm_multicast_async

        stmt = select(DeviceToken.token).where(DeviceToken.is_active.is_(True))
        if user_ids is not None:
            stmt = stmt.where(DeviceToken.user_id.in_(user_ids))

        res = await session.execute(stmt)
        tokens = list(dict.fromkeys([t for t in res.scalars().all() if t and t.strip()]))
        if not tokens:
            logger.info("No active device tokens found for broadcast")
            return 0

        success = await send_fcm_multicast_async(tokens, title, body, data)
        logger.info(
            "Broadcast push notification delivered to %d/%d active devices across all roles",
            success,
            len(tokens),
        )
        return success
    except Exception as exc:
        logger.error("Broadcast push notification failed: %s", exc)
        return 0


async def create_payment_notification(
    session: AsyncSession,
    *,
    category: str,  # "contribution" | "group_payment" | "ad_payment" | "payment"
    amount: Decimal | float,
    group_id: UUID | None = None,
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
    """Create a payment or contribution notification in the database and trigger FCM pushes:
    
    a) Payer / Member: Receives a personalized confirmation ("आपका ₹X का सहयोग सफलतापूर्वक प्राप्त हुआ, धन्यवाद!")
    b) Group Admins & Super Admins: Receive an admin alert ("₹X का नया सहयोग प्राप्त हुआ - {donor_name}")
    """
    try:
        amt_float = float(amount)
        amt_disp = int(amt_float) if amt_float.is_integer() else f"{amt_float:.2f}"
        amt_inr = f"₹{amt_disp}"

        # 1. Determine titles and bodies for both Member and Admins
        if category == "contribution":
            member_push_title = "सहयोग प्राप्त हुआ"
            member_push_body = f"आपका {amt_inr} का सहयोग सफलतापूर्वक प्राप्त हुआ, धन्यवाद!"

            d_name = donor_name or "एक सदस्य"
            grp_suffix = f" ({group_name})" if group_name else ""
            admin_push_title = "नया सहयोग प्राप्त हुआ"
            admin_push_body = f"{amt_inr} का नया सहयोग प्राप्त हुआ - {d_name}{grp_suffix}"

            notification_type = "CONTRIBUTION"
            default_link = "/admin/contributions"
        elif category == "group_payment":
            member_push_title = "भुगतान प्राप्त हुआ"
            member_push_body = f"आपका {amt_inr} का भुगतान सफलतापूर्वक प्राप्त हुआ, धन्यवाद!"

            grp_str = f" - {group_name}" if group_name else ""
            admin_push_title = "नया भुगतान प्राप्त हुआ"
            admin_push_body = f"{amt_inr} का नया भुगतान प्राप्त हुआ{grp_str}"

            notification_type = "GROUP_PAYMENT"
            default_link = "/admin/payments"
        elif category == "ad_payment":
            member_push_title = "विज्ञापन भुगतान प्राप्त हुआ"
            member_push_body = f"आपका {amt_inr} का विज्ञापन भुगतान सफलतापूर्वक प्राप्त हुआ, धन्यवाद!"

            ad_target = donor_name or purpose or "विज्ञापन"
            admin_push_title = "नया विज्ञापन भुगतान प्राप्त हुआ"
            admin_push_body = f"{amt_inr} का नया विज्ञापन भुगतान प्राप्त हुआ - {ad_target}"

            notification_type = "AD_PAYMENT"
            default_link = "/admin/advertisements"
        else:
            member_push_title = "भुगतान प्राप्त हुआ"
            member_push_body = f"आपका {amt_inr} का भुगतान सफलतापूर्वक प्राप्त हुआ, धन्यवाद!"

            admin_push_title = "नया भुगतान प्राप्त हुआ"
            admin_push_body = f"{amt_inr} का नया भुगतान प्राप्त हुआ"

            notification_type = "PAYMENT"
            default_link = "/admin/payments"

        meta = {
            "category": category,
            "amount": amt_float,
            "donor_name": donor_name,
            "group_name": group_name,
            "group_id": str(group_id) if group_id else None,
            "purpose": purpose,
            "receipt_no": receipt_no,
            "transaction_id": transaction_id,
            "order_id": order_id,
            "link": link or default_link,
        }

        # 2. Create DB notification for the Member (if recipient_id is present)
        member_notif: Notification | None = None
        if recipient_id:
            member_notif = Notification(
                recipient_id=recipient_id,
                title=member_push_title,
                body=member_push_body,
                data=json.dumps({**meta, "target": "member"}),
                notification_type=notification_type,
                entity_type="payment" if category != "contribution" else "contribution",
                entity_id=payment_id,
                sent_at=sent_at or datetime.now(UTC),
            )
            session.add(member_notif)

        # 3. Create DB notification for Admins (recipient_id=None so it appears in Admin panel)
        admin_notif = Notification(
            recipient_id=None,
            title=admin_push_title,
            body=admin_push_body,
            data=json.dumps({**meta, "target": "admin"}),
            notification_type=notification_type,
            entity_type="payment" if category != "contribution" else "contribution",
            entity_id=payment_id,
            sent_at=sent_at or datetime.now(UTC),
        )
        session.add(admin_notif)
        await session.flush()

        # 4. Trigger FCM Push Notifications
        try:
            # a) Send Member Push Notification
            if recipient_id:
                member_payload = {
                    "category": category,
                    "amount": str(amount),
                    "donor_name": donor_name or "",
                    "group_name": group_name or "",
                    "receipt_no": receipt_no or "",
                    "order_id": order_id or "",
                    "notification_type": notification_type,
                    "notification_id": str(member_notif.id if member_notif else admin_notif.id),
                    "target": "member",
                }
                await send_push_notification_to_user(
                    session,
                    recipient_id,
                    title=member_push_title,
                    body=member_push_body,
                    data=member_payload,
                )

            # b) Send Admin Push Notification (to GROUP_ADMIN of that group + all SUPER_ADMINs)
            admin_user_ids = await get_admin_user_ids(session, group_id=group_id)
            target_admin_ids = [uid for uid in admin_user_ids if uid != recipient_id]

            if target_admin_ids:
                admin_payload = {
                    "category": category,
                    "amount": str(amount),
                    "donor_name": donor_name or "",
                    "group_name": group_name or "",
                    "receipt_no": receipt_no or "",
                    "order_id": order_id or "",
                    "notification_type": notification_type,
                    "notification_id": str(admin_notif.id),
                    "target": "admin",
                }
                await send_push_notification_to_users(
                    session,
                    target_admin_ids,
                    title=admin_push_title,
                    body=admin_push_body,
                    data=admin_payload,
                )
        except Exception as push_err:
            logger.warning("FCM push alerts trigger failed (DB notification saved): %s", push_err)

        return member_notif or admin_notif
    except Exception as exc:
        logger.warning("Could not create payment notification: %s", exc)
        return None
