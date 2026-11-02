from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advertisement_subscription_plan import AdvertisementSubscriptionPlan


class AdvertisementSubscriptionPlanRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_plans(
        self, *, active_only: bool = True
    ) -> list[AdvertisementSubscriptionPlan]:
        stmt = select(AdvertisementSubscriptionPlan)
        if active_only:
            stmt = stmt.where(AdvertisementSubscriptionPlan.is_active.is_(True))
        result = await self.session.execute(
            stmt.order_by(AdvertisementSubscriptionPlan.months)
        )
        return list(result.scalars().all())

    async def get_by_months(self, months: int) -> AdvertisementSubscriptionPlan | None:
        result = await self.session.execute(
            select(AdvertisementSubscriptionPlan).where(
                AdvertisementSubscriptionPlan.months == months
            )
        )
        return result.scalar_one_or_none()
