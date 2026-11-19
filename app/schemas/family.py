from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.utils.helpers import format_to_dd_mm_yyyy, parse_any_date


class FamilyMemberCreate(BaseModel):
    name: str
    relationship_type: str
    date_of_birth: date | None = None
    occupation: str | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _parse_dob(cls, v: Any) -> date | None:
        return parse_any_date(v)
    contact_phone: str | None = None
    is_visible: bool = True


class FamilyMemberUpdate(BaseModel):
    name: str | None = None
    relationship_type: str | None = None
    date_of_birth: date | None = None
    occupation: str | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _parse_dob(cls, v: Any) -> date | None:
        return parse_any_date(v)
    contact_phone: str | None = None
    is_visible: bool | None = None


class FamilyMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID
    name: str
    name_hi: str | None = None
    relationship_type: str
    date_of_birth: str | None = None
    occupation: str | None = None
    occupation_hi: str | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _format_dob(cls, v: Any) -> str | None:
        return format_to_dd_mm_yyyy(v)
    contact_phone: str | None = None
    is_visible: bool
    is_approved: bool
    created_at: datetime
    updated_at: datetime
