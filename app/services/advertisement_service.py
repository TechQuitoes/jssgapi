from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestException, NotFoundException
from app.models.advertisement import Advertisement
from app.models.advertisement_subscription_plan import AdvertisementSubscriptionPlan
from app.repositories.advertisement_subscription_plan_repository import (
    AdvertisementSubscriptionPlanRepository,
)
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import MIME_SUFFIX, validate_image_upload

logger = logging.getLogger(__name__)

#: Recommended: 1200 x 628 px, Max 2MB
MAX_COVER_IMAGE_SIZE = 2 * 1024 * 1024
COVER_OBJECT_KEY_TEMPLATE = "advertisements/{ad_id}/cover/{unique}{suffix}"

#: Recommended: 1200 x 1800 px, Max 5MB
MAX_LARGE_IMAGE_SIZE = 5 * 1024 * 1024
LARGE_OBJECT_KEY_TEMPLATE = "advertisements/{ad_id}/large/{unique}{suffix}"


class AdvertisementService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.storage = storage or get_storage()
        self.plans = AdvertisementSubscriptionPlanRepository(session)

    async def list_subscription_plans(
        self, *, active_only: bool = True
    ) -> list[AdvertisementSubscriptionPlan]:
        return await self.plans.list_plans(active_only=active_only)

    async def get_subscription_plan(self, months: int) -> AdvertisementSubscriptionPlan:
        plan = await self.plans.get_by_months(months)
        if plan is None or not plan.is_active:
            raise BadRequestException(
                "Select a valid advertisement subscription plan",
                "INVALID_SUBSCRIPTION_PLAN",
            )
        return plan

    async def update_subscription_prices(
        self, updates: list[tuple[int, Decimal]]
    ) -> list[AdvertisementSubscriptionPlan]:
        plans: list[AdvertisementSubscriptionPlan] = []
        seen: set[int] = set()
        for months, price in updates:
            if months in seen:
                raise BadRequestException(
                    f"Duplicate price update for {months} month(s)",
                    "DUPLICATE_SUBSCRIPTION_PLAN",
                )
            seen.add(months)
            plan = await self.plans.get_by_months(months)
            if plan is None:
                raise NotFoundException("Advertisement subscription plan", str(months))
            if price <= 0:
                raise BadRequestException(
                    "Subscription price must be greater than zero",
                    "INVALID_SUBSCRIPTION_PRICE",
                )
            plan.price = price
            plans.append(plan)
        await self.session.flush()
        for plan in plans:
            await self.session.refresh(plan)
        return plans

    async def upload_cover(
        self,
        ad: Advertisement,
        *,
        file_name: str | None,
        content: bytes,
        mime_type: str | None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        """Upload or replace advertisement cover image (max 2MB, 1200x628)."""
        if len(content) > MAX_COVER_IMAGE_SIZE:
            raise BadRequestException(
                "Cover image is larger than 2MB limit",
                "COVER_IMAGE_TOO_LARGE",
            )

        safe_name, safe_mime = validate_image_upload(
            file_name, mime_type, content, error_code="INVALID_COVER_IMAGE"
        )
        object_key = COVER_OBJECT_KEY_TEMPLATE.format(
            ad_id=ad.id,
            unique=uuid.uuid4().hex,
            suffix=MIME_SUFFIX[safe_mime],
        )
        previous_key = (ad.cover_image_object_key or "").strip() or None

        media = await MediaService(self.session, storage=self.storage).upload(
            owner_type="advertisement",
            owner_id=ad.id,
            file_name=safe_name,
            content=content,
            mime_type=safe_mime,
            is_public=True,
            group_id=ad.group_id,
            uploaded_by=uploaded_by,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
            object_key=object_key,
        )

        ad.cover_image_object_key = object_key
        ad.cover_image_url = media.url
        if not ad.media_url:
            ad.media_url = media.url

        if previous_key:
            await self._delete_object_best_effort(previous_key)

        return media.url

    async def upload_large(
        self,
        ad: Advertisement,
        *,
        file_name: str | None,
        content: bytes,
        mime_type: str | None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        """Upload or replace advertisement large image (max 5MB, 1200x1800)."""
        if len(content) > MAX_LARGE_IMAGE_SIZE:
            raise BadRequestException(
                "Large image is larger than 5MB limit",
                "LARGE_IMAGE_TOO_LARGE",
            )

        safe_name, safe_mime = validate_image_upload(
            file_name, mime_type, content, error_code="INVALID_LARGE_IMAGE"
        )
        object_key = LARGE_OBJECT_KEY_TEMPLATE.format(
            ad_id=ad.id,
            unique=uuid.uuid4().hex,
            suffix=MIME_SUFFIX[safe_mime],
        )
        previous_key = (ad.large_image_object_key or "").strip() or None

        media = await MediaService(self.session, storage=self.storage).upload(
            owner_type="advertisement",
            owner_id=ad.id,
            file_name=safe_name,
            content=content,
            mime_type=safe_mime,
            is_public=True,
            group_id=ad.group_id,
            uploaded_by=uploaded_by,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
            object_key=object_key,
        )

        ad.large_image_object_key = object_key
        ad.large_image_url = media.url

        if previous_key:
            await self._delete_object_best_effort(previous_key)

        return media.url

    async def remove_cover(self, ad: Advertisement) -> None:
        """Remove advertisement cover image."""
        key = (ad.cover_image_object_key or "").strip() or None
        ad.cover_image_url = None
        ad.cover_image_object_key = None
        if key:
            await self._discard_object(key)

    async def remove_large(self, ad: Advertisement) -> None:
        """Remove advertisement large image."""
        key = (ad.large_image_object_key or "").strip() or None
        ad.large_image_url = None
        ad.large_image_object_key = None
        if key:
            await self._discard_object(key)

    async def delete_all_for_ad(self, ad: Advertisement) -> None:
        """Clean up all media objects associated with the advertisement."""
        await self.remove_cover(ad)
        await self.remove_large(ad)

    async def _delete_object_best_effort(self, object_key: str) -> None:
        await self._discard_object(object_key)

    async def _discard_object(self, object_key: str) -> None:
        try:
            self.storage.delete_object(object_key)
        except Exception:
            logger.warning(
                "Could not delete advertisement media object %s",
                object_key,
                exc_info=True,
            )
        media = await MediaRepository(self.session).get_by_object_key(object_key)
        if media is not None:
            await MediaRepository(self.session).delete(media)
