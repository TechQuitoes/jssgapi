"""Schemas for the geo master-data endpoints."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GeoStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lgd_code: int
    name: str
    #: Project-owned two-letter Group-ID prefix (not an LGD code).
    code: str
    is_union_territory: bool
    is_active: bool


class GeoCityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    #: LGD code when the city is backed by a ``geo_cities`` row; ``None`` for
    #: RTO-only names.
    lgd_code: int | None = None
    name: str
    state_id: UUID
    district_id: UUID | None = None
    district_name: str | None = None
    ulb_type_code: int | None = None
    census_2011_code: str | None = None
    #: RTO-aligned Group-ID city code, e.g. ``09``.
    city_code: str
    #: ``state.code + city_code``, e.g. ``MP09``.
    prefix: str
    is_active: bool
    #: Two-letter state code, e.g. ``MP``.
    state_code: str | None = None
    #: Canonical state name, e.g. ``Madhya Pradesh``.
    state_name: str | None = None
    #: Other names that share this RTO code.
    aliases: list[str] = Field(default_factory=list)
    #: ``rto`` or ``lgd`` (legacy DB-backed city).
    source: str = "lgd"
