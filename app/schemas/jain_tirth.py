from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_validator

from app.core.constants import EventStatus


class JainTirthCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    location: str | None = None
    state: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    contact_person_name_1: str | None = Field(default=None, min_length=2, max_length=255)
    contact_person_phone_1: str | None = Field(default=None, min_length=1, max_length=20)
    contact_person_name_2: str | None = Field(default=None, min_length=2, max_length=255)
    contact_person_phone_2: str | None = Field(default=None, min_length=1, max_length=20)
    website_url: str | None = Field(default=None, max_length=500)
    cover_image_url: str | None = None
    cover_image_thumb_url: str | None = None
    status: EventStatus = EventStatus.PUBLISHED
    is_public: bool = True
    group_id: UUID | None = None

    @field_validator("state", "city")
    @classmethod
    def _reject_blank_place(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("website_url")
    @classmethod
    def _reject_blank_website(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator(
        "contact_person_name_1",
        "contact_person_phone_1",
        "contact_person_name_2",
        "contact_person_phone_2",
    )
    @classmethod
    def _trim_contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _require_contact_pairs(self) -> JainTirthCreate:
        if (self.contact_person_name_1 is None) != (
            self.contact_person_phone_1 is None
        ):
            raise ValueError(
                "contact_person_name_1 and contact_person_phone_1 must be "
                "provided together"
            )
        if (self.contact_person_name_2 is None) != (
            self.contact_person_phone_2 is None
        ):
            raise ValueError(
                "contact_person_name_2 and contact_person_phone_2 must be "
                "provided together"
            )
        if (self.state is None) != (self.city is None):
            raise ValueError("state and city must be provided together")
        return self


class JainTirthUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    location: str | None = None
    state: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    contact_person_name_1: str | None = Field(default=None, min_length=2, max_length=255)
    contact_person_phone_1: str | None = Field(default=None, min_length=1, max_length=20)
    contact_person_name_2: str | None = Field(default=None, min_length=2, max_length=255)
    contact_person_phone_2: str | None = Field(default=None, min_length=1, max_length=20)
    website_url: str | None = Field(default=None, max_length=500)
    cover_image_url: str | None = None
    cover_image_thumb_url: str | None = None
    status: EventStatus | None = None
    is_public: bool | None = None
    group_id: UUID | None = None

    @field_validator("state", "city")
    @classmethod
    def _reject_blank_place(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("website_url")
    @classmethod
    def _reject_blank_website(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator(
        "contact_person_name_1",
        "contact_person_phone_1",
        "contact_person_name_2",
        "contact_person_phone_2",
    )
    @classmethod
    def _trim_contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _finalise(self) -> JainTirthUpdate:
        not_clearable = {"title", "status", "is_public"}
        cleared = not_clearable & {
            name for name in self.model_fields_set if getattr(self, name) is None
        }
        if cleared:
            raise ValueError(f"Cannot be set to null: {', '.join(sorted(cleared))}")
        if (self.state is None) != (self.city is None) and (
            "state" in self.model_fields_set or "city" in self.model_fields_set
        ):
            raise ValueError("state and city must be provided together")
        if (self.contact_person_name_1 is None) != (
            self.contact_person_phone_1 is None
        ) and (
            "contact_person_name_1" in self.model_fields_set
            or "contact_person_phone_1" in self.model_fields_set
        ):
            raise ValueError(
                "contact_person_name_1 and contact_person_phone_1 must be "
                "provided together"
            )
        if (self.contact_person_name_2 is None) != (
            self.contact_person_phone_2 is None
        ) and (
            "contact_person_name_2" in self.model_fields_set
            or "contact_person_phone_2" in self.model_fields_set
        ):
            raise ValueError(
                "contact_person_name_2 and contact_person_phone_2 must be "
                "provided together"
            )
        return self


class JainTirthImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    jain_tirth_id: UUID
    url: str
    thumb_url: str | None = None
    position: int
    created_at: datetime

    @model_validator(mode="after")
    def _fill_thumb_url(self) -> JainTirthImageOut:
        if not self.thumb_url:
            self.thumb_url = self.url
        return self


class JainTirthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    location: str | None = None
    state: str | None = None
    city: str | None = None
    contact_person_name_1: str | None = None
    contact_person_phone_1: str | None = None
    contact_person_name_2: str | None = None
    contact_person_phone_2: str | None = None
    website_url: str | None = None
    cover_image_url: str | None = None
    cover_image_thumb_url: str | None = None
    thumb_url: str | None = None
    status: str
    is_public: bool
    group_id: UUID | None = None
    image_count: int = 0
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _fill_thumb_url(self) -> JainTirthOut:
        if not self.thumb_url:
            self.thumb_url = self.cover_image_thumb_url or self.cover_image_url
        return self
    status: str
    is_public: bool
    group_id: UUID | None = None
    image_count: int = 0
    created_at: datetime
    updated_at: datetime


class JainTirthDetailOut(JainTirthOut):
    images: list[JainTirthImageOut] = Field(default_factory=list)
