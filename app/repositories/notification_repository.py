from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy import false as sql_false

from app.core.constants import RoleName, resolve_role_name
from app.models.notification import DeviceToken, Notification
from app.models.user import User
from app.repositories.base_repository import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    def _apply_filters(
        self,
        user_id: UUID | None = None,
        category: str | None = None,
        current_user: User | None = None,
        role: str | RoleName | None = None,
        group_ids: list[UUID] | None = None,
    ):
        conds = []

        # 1. Resolve user_id and effective role
        uid = current_user.id if current_user else user_id
        if uid is None:
            return [sql_false()]

        effective_role: RoleName = RoleName.MEMBER
        if current_user and current_user.role:
            resolved = resolve_role_name(current_user.role.name)
            if resolved:
                effective_role = resolved
        elif role:
            resolved = resolve_role_name(str(role))
            if resolved:
                effective_role = resolved

        # 2. Apply role-based scoping
        if effective_role == RoleName.SUPER_ADMIN:
            # Super Admin sees everything: personal notifications + all admin/broadcast notifications
            conds.append(
                or_(
                    Notification.recipient_id == uid,
                    Notification.recipient_id.is_(None),
                )
            )
        elif effective_role == RoleName.GROUP_ADMIN:
            # Group Admin can see:
            # a) Direct personal notifications (recipient_id == uid)
            # b) For their own group:
            #    - Group fee payments (GROUP_PAYMENT / PAYMENT)
            #    - Group members' contributions (CONTRIBUTION)
            # Excluded: AD_PAYMENT (advertisements are strictly Super Admin),
            # other groups' notifications, and general non-group alerts.
            allowed_group_types = or_(
                Notification.notification_type.in_(
                    ["GROUP_PAYMENT", "CONTRIBUTION", "PAYMENT"]
                ),
                Notification.data.like('%"category": "group_payment"%'),
                Notification.data.like('%"category": "contribution"%'),
            )
            exclude_ads = and_(
                Notification.notification_type != "AD_PAYMENT",
                not_(Notification.data.like('%"category": "ad_payment"%')),
            )

            if group_ids:
                group_matches = [
                    Notification.data.like(f'%"group_id": "{str(gid)}"%')
                    for gid in group_ids
                ]
                group_admin_admin_notifs = and_(
                    Notification.recipient_id.is_(None),
                    allowed_group_types,
                    exclude_ads,
                    or_(*group_matches),
                )
                conds.append(
                    or_(
                        Notification.recipient_id == uid,
                        group_admin_admin_notifs,
                    )
                )
            else:
                # Group admin without group assigned only sees direct personal notifications
                conds.append(Notification.recipient_id == uid)
        else:
            # MEMBER / GUEST:
            # "member khud jbh koi contribution krega to khud ka notification dekh sakta hai bs or koi bhi notification nhi dekh sakta hai"
            # Members can ONLY see notifications addressed to them personally.
            # Never see recipient_id is None (admin alerts / broadcasts / other groups / ads).
            conds.append(Notification.recipient_id == uid)

        # 3. Apply category filter if requested
        if category and category.lower() not in ("all", "all received"):
            cat_lower = category.lower()
            cat_upper = category.upper()

            if effective_role == RoleName.GROUP_ADMIN and cat_lower in (
                "ad_payment",
                "advertisement",
                "ad",
                "ads",
            ):
                # Group Admins cannot see advertisements
                return [sql_false()]

            if cat_lower in ("contribution", "contributions"):
                conds.append(
                    or_(
                        Notification.notification_type == "CONTRIBUTION",
                        Notification.notification_type == "contribution",
                        Notification.data.like('%"category": "contribution"%'),
                    )
                )
            elif cat_lower in (
                "group_payment",
                "fee_payment",
                "fee",
                "group_payments",
                "fees",
            ):
                conds.append(
                    or_(
                        Notification.notification_type == "GROUP_PAYMENT",
                        Notification.notification_type == "group_payment",
                        Notification.data.like('%"category": "group_payment"%'),
                    )
                )
            elif cat_lower in ("ad_payment", "advertisement", "ad", "ads"):
                conds.append(
                    or_(
                        Notification.notification_type == "AD_PAYMENT",
                        Notification.notification_type == "ad_payment",
                        Notification.data.like('%"category": "ad_payment"%'),
                    )
                )
            else:
                conds.append(
                    or_(
                        Notification.notification_type == cat_upper,
                        Notification.notification_type == cat_lower,
                        Notification.data.like(f'%"category": "{cat_lower}"%'),
                    )
                )
        return conds

    def can_user_access(
        self,
        notification: Notification,
        current_user: User,
        group_ids: list[UUID] | None = None,
    ) -> bool:
        """Check if current_user has permission to view or mark read a specific notification."""
        # 1. Direct recipient check
        if notification.recipient_id is not None:
            return notification.recipient_id == current_user.id

        # 2. Recipient is None (Admin/Broadcast notification)
        effective_role: RoleName = RoleName.MEMBER
        if current_user and current_user.role:
            resolved = resolve_role_name(current_user.role.name)
            if resolved:
                effective_role = resolved

        # Super Admin sees all admin notifications
        if effective_role == RoleName.SUPER_ADMIN:
            return True

        # Member and Guest can NEVER access admin-level notifications (recipient_id is None)
        if effective_role in (RoleName.MEMBER, RoleName.GUEST):
            return False

        # Group Admin:
        if effective_role == RoleName.GROUP_ADMIN:
            # Exclude advertisements
            if notification.notification_type == "AD_PAYMENT":
                return False
            data_str = notification.data or ""
            if '"category": "ad_payment"' in data_str:
                return False

            # Must be contribution or group payment
            is_allowed = (
                notification.notification_type
                in ("GROUP_PAYMENT", "CONTRIBUTION", "PAYMENT")
                or '"category": "group_payment"' in data_str
                or '"category": "contribution"' in data_str
            )
            if not is_allowed:
                return False

            # Must belong to one of the admin's groups
            if group_ids:
                try:
                    parsed = json.loads(data_str)
                    data_gid = parsed.get("group_id")
                    if data_gid and any(
                        str(gid).lower() == str(data_gid).lower() for gid in group_ids
                    ):
                        return True
                except Exception:
                    pass
                for gid in group_ids:
                    if str(gid).lower() in data_str.lower():
                        return True
            return False

        return False

    async def list_for_user(
        self,
        user_id: UUID | None,
        limit: int,
        offset: int,
        category: str | None = None,
        current_user: User | None = None,
        group_ids: list[UUID] | None = None,
    ) -> list[Notification]:
        filters = self._apply_filters(
            user_id=user_id,
            category=category,
            current_user=current_user,
            group_ids=group_ids,
        )
        result = await self.session.execute(
            select(Notification)
            .where(*filters)
            .order_by(
                Notification.sent_at.desc().nullslast(), Notification.created_at.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(
        self,
        user_id: UUID | None,
        category: str | None = None,
        current_user: User | None = None,
        group_ids: list[UUID] | None = None,
    ) -> int:
        filters = self._apply_filters(
            user_id=user_id,
            category=category,
            current_user=current_user,
            group_ids=group_ids,
        )
        result = await self.session.execute(
            select(func.count(Notification.id)).where(*filters)
        )
        return result.scalar() or 0

    async def count_unread(
        self,
        user_id: UUID | None,
        category: str | None = None,
        current_user: User | None = None,
        group_ids: list[UUID] | None = None,
    ) -> int:
        if current_user is None and user_id is None:
            return 0
        filters = self._apply_filters(
            user_id=user_id,
            category=category,
            current_user=current_user,
            group_ids=group_ids,
        )
        result = await self.session.execute(
            select(func.count(Notification.id)).where(
                *filters,
                Notification.is_read.is_(False),
            )
        )
        return result.scalar() or 0


class DeviceTokenRepository(BaseRepository[DeviceToken]):
    model = DeviceToken

    async def get_by_token(self, token: str) -> DeviceToken | None:
        result = await self.session.execute(
            select(DeviceToken).where(DeviceToken.token == token)
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id) -> list[DeviceToken]:
        result = await self.session.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id, DeviceToken.is_active.is_(True)
            )
        )
        return list(result.scalars().all())
