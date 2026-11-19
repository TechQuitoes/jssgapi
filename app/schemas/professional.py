from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProfessionalInfoCreate(BaseModel):
    occupation: str | None = None
    occupation_hi: str | None = None
    company_name: str | None = None
    company_name_hi: str | None = None
    designation: str | None = None
    designation_hi: str | None = None
    industry: str | None = None
    business_phone: str | None = None
    business_email: str | None = None
    business_address: str | None = None
    website: str | None = None
    is_visible_in_directory: bool = True


class ProfessionalInfoUpdate(BaseModel):
    occupation: str | None = None
    occupation_hi: str | None = None
    company_name: str | None = None
    company_name_hi: str | None = None
    designation: str | None = None
    designation_hi: str | None = None
    industry: str | None = None
    business_phone: str | None = None
    business_email: str | None = None
    business_address: str | None = None
    website: str | None = None
    is_visible_in_directory: bool | None = None


class ProfessionalInfoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID
    occupation: str | None = None
    occupation_hi: str | None = None
    company_name: str | None = None
    company_name_hi: str | None = None
    designation: str | None = None
    designation_hi: str | None = None
    industry: str | None = None
    business_phone: str | None = None
    business_email: str | None = None
    business_address: str | None = None
    website: str | None = None
    is_visible_in_directory: bool
    created_at: datetime
    updated_at: datetime
