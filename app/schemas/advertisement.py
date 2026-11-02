from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionDurationOption(BaseModel):
    months: int
    label: str
    description: str
    price: Decimal
    currency: str = "INR"
    is_default: bool = False


class SubscriptionPriceUpdate(BaseModel):
    months: int = Field(ge=1)
    price: Decimal = Field(gt=0)


class SubscriptionPricingUpdate(BaseModel):
    plans: list[SubscriptionPriceUpdate] = Field(min_length=1)


class AdvertisementCreate(BaseModel):
    title: str
    person_name: str | None = None
    mobile_number: str | None = None
    group_id: UUID | None = None
    start_date: datetime | date | None = None
    end_date: datetime | date | None = None
    subscription_months: int = 1
    cover_image_url: str | None = None
    large_image_url: str | None = None
    amount: Decimal | float | None = None
    payment_status: str | None = None
    payment_id: UUID | None = None
    description: str | None = None
    media_url: str | None = None
    media_type: str = "IMAGE"
    placement: str = "HOME_BANNER"
    target_url: str | None = None
    status: str | None = None
    is_active: bool = True


class AdvertisementUpdate(BaseModel):
    title: str | None = None
    person_name: str | None = None
    mobile_number: str | None = None
    group_id: UUID | None = None
    start_date: datetime | date | None = None
    end_date: datetime | date | None = None
    subscription_months: int | None = None
    cover_image_url: str | None = None
    large_image_url: str | None = None
    amount: Decimal | float | None = None
    payment_status: str | None = None
    payment_id: UUID | None = None
    description: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    placement: str | None = None
    target_url: str | None = None
    status: str | None = None
    is_active: bool | None = None


class AdvertisementApproveRequest(BaseModel):
    approve: bool
    rejection_reason: str | None = None


class AdvertisementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    person_name: str | None = None
    mobile_number: str | None = None
    description: str | None = None
    media_url: str | None = None
    media_type: str
    cover_image_url: str | None = None
    large_image_url: str | None = None
    amount: Decimal | float | None = None
    subscription_months: int | None = 1
    subscription_plan_id: UUID | None = None
    currency: str | None = None
    purchased_at: datetime | None = None
    subscription_start_date: datetime | None = None
    subscription_expiry_date: datetime | None = None
    placement: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    target_url: str | None = None
    status: str
    is_active: bool
    payment_status: str | None = None
    payment_id: UUID | None = None
    group_id: UUID | None = None
    group_name: str | None = None
    advertiser_id: UUID | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime
