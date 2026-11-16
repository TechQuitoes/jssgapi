from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.group import SocialGroup
from app.models.group_payment_obligation import GroupPaymentObligation
from app.models.payment import Payment, PaymentTransaction
from app.repositories.base_repository import BaseRepository


class GroupPaymentObligationRepository(BaseRepository[GroupPaymentObligation]):
    model = GroupPaymentObligation

    async def get_by_group_and_period(
        self, group_id: uuid.UUID, billing_period: str
    ) -> GroupPaymentObligation | None:
        stmt = (
            select(GroupPaymentObligation)
            .where(
                GroupPaymentObligation.group_id == group_id,
                GroupPaymentObligation.billing_period == billing_period,
            )
            .options(
                selectinload(GroupPaymentObligation.payments),
                selectinload(GroupPaymentObligation.transactions),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(
        self, obligation_id: uuid.UUID
    ) -> GroupPaymentObligation | None:
        """Fetch obligation with a row-level lock (FOR UPDATE) to serialize concurrent payments."""
        stmt = (
            select(GroupPaymentObligation)
            .where(GroupPaymentObligation.id == obligation_id)
            .with_for_update()
            .options(
                selectinload(GroupPaymentObligation.payments),
                selectinload(GroupPaymentObligation.transactions),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_payment_link_id(
        self, payment_link_id: str
    ) -> GroupPaymentObligation | None:
        stmt = (
            select(GroupPaymentObligation)
            .where(GroupPaymentObligation.active_payment_link_id == payment_link_id)
            .options(
                selectinload(GroupPaymentObligation.payments),
                selectinload(GroupPaymentObligation.transactions),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_payment_link_id_for_update(
        self, payment_link_id: str
    ) -> GroupPaymentObligation | None:
        stmt = (
            select(GroupPaymentObligation)
            .where(GroupPaymentObligation.active_payment_link_id == payment_link_id)
            .with_for_update()
            .options(
                selectinload(GroupPaymentObligation.payments),
                selectinload(GroupPaymentObligation.transactions),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_period(
        self,
        billing_period: str,
        *,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[GroupPaymentObligation], int]:
        stmt = select(GroupPaymentObligation).where(
            GroupPaymentObligation.billing_period == billing_period
        )
        if search:
            stmt = stmt.where(GroupPaymentObligation.group_name.ilike(f"%{search.strip()}%"))

        total = (
            await self.session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        result = await self.session.execute(
            stmt.order_by(GroupPaymentObligation.group_name.asc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total
