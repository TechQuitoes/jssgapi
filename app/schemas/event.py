from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.constants import EventStatus, EventType
from app.utils.event_datetime import (
    classify_time_frame,
    format_event_date,
    format_event_time,
    get_app_tz,
    parse_event_date,
    parse_event_time,
)

#: A phone number, with the separators people actually type.
_PHONE_CLEAN = re.compile(r"[\s\-()]+")
_PHONE_VALID = re.compile(r"^\+?[0-9]{7,15}$")


def _clean_phone(value: str | None) -> str | None:
    """Normalise a phone number and reject anything that is not one."""
    if value is None:
        return None
    cleaned = _PHONE_CLEAN.sub("", value.strip())
    if not cleaned:
        return None
    if not _PHONE_VALID.match(cleaned):
        raise ValueError(f"Invalid contact number: {value!r}")
    return cleaned


def _ordered_group_ids(values: list[UUID] | None) -> list[UUID]:
    ordered: list[UUID] = []
    for value in values or []:
        if value not in ordered:
            ordered.append(value)
    return ordered


def _as_local(value: datetime) -> datetime:
    """A datetime in the platform timezone; a naive one is read as UTC."""
    tz = get_app_tz()
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


class EventCreate(BaseModel):
    """Create an event.

    The date and time are local wall-clock values (``dd/mm/yyyy`` and
    ``HH:MM``). A client that still sends the old ``starts_at`` timestamp
    instead is accepted: the date and time are derived from it.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    title_hi: str | None = Field(default=None, max_length=255)
    description: str | None = None
    event_date: date | None = None
    event_time: time | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_name: str | None = Field(default=None, max_length=255)
    venue_address: str | None = None
    cover_image_url: str | None = None
    event_type: EventType = EventType.OTHER
    event_type_other: str | None = Field(default=None, max_length=255)
    host_name: str | None = Field(default=None, max_length=255)
    host_contact_number: str | None = Field(default=None, max_length=20)
    status: EventStatus = EventStatus.PUBLISHED
    is_public: bool = True
    max_attendees: int | None = Field(default=None, ge=1)
    registration_required: bool = False
    group_id: UUID | None = None
    group_ids: list[UUID] | None = None
    location_id: UUID | None = None

    @field_validator("event_date", mode="before")
    @classmethod
    def _check_date(cls, value: Any) -> Any:
        return None if value is None else parse_event_date(value)

    @field_validator("event_time", mode="before")
    @classmethod
    def _check_time(cls, value: Any) -> Any:
        return None if value is None else parse_event_time(value)

    @field_validator("host_contact_number", mode="before")
    @classmethod
    def _check_phone(cls, value: Any) -> Any:
        return _clean_phone(value)

    @model_validator(mode="after")
    def _finalise(self) -> EventCreate:
        if self.event_date is None or self.event_time is None:
            if self.starts_at is None:
                raise ValueError("event_date and event_time are required")
            local = _as_local(self.starts_at)
            self.event_date = local.date()
            self.event_time = local.time()
        if self.event_type is EventType.OTHER:
            if not (self.event_type_other or "").strip():
                raise ValueError(
                    "event_type_other is required when event_type is OTHER"
                )
        else:
            # The label only means anything alongside OTHER, so it never
            # survives a switch to a named type.
            self.event_type_other = None
        return self

    def requested_group_ids(self) -> list[UUID]:
        """Ordered selection, preferring ``group_ids`` over legacy ``group_id``."""
        source = self.group_ids
        if source is None and self.group_id is not None:
            source = [self.group_id]
        return _ordered_group_ids(source)


class EventUpdate(BaseModel):
    """Patch an event. Every field is optional; only what is sent changes."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    title_hi: str | None = Field(default=None, max_length=255)
    description: str | None = None
    event_date: date | None = None
    event_time: time | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_name: str | None = Field(default=None, max_length=255)
    venue_address: str | None = None
    cover_image_url: str | None = None
    event_type: EventType | None = None
    event_type_other: str | None = Field(default=None, max_length=255)
    host_name: str | None = Field(default=None, max_length=255)
    host_contact_number: str | None = Field(default=None, max_length=20)
    status: EventStatus | None = None
    is_public: bool | None = None
    max_attendees: int | None = Field(default=None, ge=1)
    registration_required: bool | None = None
    group_id: UUID | None = None
    group_ids: list[UUID] | None = None
    location_id: UUID | None = None

    @field_validator("event_date", mode="before")
    @classmethod
    def _check_date(cls, value: Any) -> Any:
        return None if value is None else parse_event_date(value)

    @field_validator("event_time", mode="before")
    @classmethod
    def _check_time(cls, value: Any) -> Any:
        return None if value is None else parse_event_time(value)

    @field_validator("host_contact_number", mode="before")
    @classmethod
    def _check_phone(cls, value: Any) -> Any:
        return _clean_phone(value)

    @model_validator(mode="after")
    def _finalise(self) -> EventUpdate:
        # A field that is sent explicitly as null cannot be cleared: these
        # columns are required, so refusing the request beats writing a null
        # and failing deep inside the database.
        not_clearable = {
            "title",
            "event_date",
            "event_time",
            "status",
            "is_public",
            "registration_required",
            "event_type",
        }
        cleared = not_clearable & {
            name for name in self.model_fields_set if getattr(self, name) is None
        }
        if cleared:
            raise ValueError(
                f"Cannot be set to null: {', '.join(sorted(cleared))}"
            )
        if self.event_type is EventType.OTHER:
            if "event_type_other" not in self.model_fields_set or not (
                self.event_type_other or ""
            ).strip():
                raise ValueError(
                    "event_type_other is required when event_type is OTHER"
                )
        elif self.event_type is not None:
            self.event_type_other = None
        return self

    def requested_group_ids(self) -> list[UUID] | None:
        """The ordered selection sent, or ``None`` when no group field changed."""
        if "group_ids" in self.model_fields_set:
            source = self.group_ids
        elif "group_id" in self.model_fields_set:
            source = [self.group_id] if self.group_id is not None else []
        else:
            return None
        return _ordered_group_ids(source)


class EventImageOut(BaseModel):
    """One image in an event's gallery."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    url: str
    position: int
    created_at: datetime


class EventOut(BaseModel):
    """An event as the API returns it.

    ``event_date`` and ``event_time`` are always the strings a person would
    read (``dd/mm/yyyy`` and ``HH:MM``), ``starts_at`` stays the machine
    readable instant, and ``time_frame`` says where the event sits relative to
    now.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    title_hi: str | None = None
    description: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    cover_image_url: str | None = None
    event_type: str
    event_type_other: str | None = None
    host_id: UUID | None = None
    host_name: str | None = None
    host_contact_number: str | None = None
    time_frame: str | None = None
    status: str
    is_public: bool
    max_attendees: int | None = None
    registration_required: bool
    group_id: UUID | None = None
    group_ids: list[UUID] = Field(default_factory=list)
    location_id: UUID | None = None
    image_count: int = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("event_date", mode="before")
    @classmethod
    def _format_date(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        return format_event_date(value)

    @field_validator("event_time", mode="before")
    @classmethod
    def _format_time(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        return format_event_time(value)

    @model_validator(mode="after")
    def _compute_time_frame(self) -> EventOut:
        if self.event_date and self.event_time:
            frame = classify_time_frame(
                parse_event_date(self.event_date), parse_event_time(self.event_time)
            )
            self.time_frame = frame.value
        return self


class EventDetailOut(EventOut):
    """An event together with its gallery."""

    images: list[EventImageOut] = Field(default_factory=list)
