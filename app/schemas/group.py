from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.profile import MemberGroupMembershipOut, ProfileGroupItem


class SocialGroupCreate(BaseModel):
    """``POST /groups`` body.

    ``state`` and ``city`` are mandatory and are validated against the backend's
    LGD-sourced geo master data - they are what the generated Group ID
    (``MP09001``) is built from. A client-sent ``group_id`` is accepted but
    **ignored**: the server always generates the ID, so a stale or forged value
    can never end up on the record.

    ``admin_name`` and ``admin_phone_number`` are optional as a pair. When both
    are supplied, the phone's account is created (or reused) and made this
    group's default ``GROUP_ADMIN`` in the same request.
    """

    name: str
    description: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str = "ACTIVE"
    is_active: bool = True
    location_id: UUID | None = None
    state: str
    city: str
    group_id: str | None = None
    admin_name: str | None = Field(
        default=None,
        min_length=2,
        max_length=255,
        validation_alias=AliasChoices("admin_name", "admin_full_name"),
    )
    admin_phone_number: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices(
            "admin_phone_number", "admin_phone", "admin_mobile_number"
        ),
    )

    @field_validator("state", "city")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Trim, then refuse missing/empty/whitespace-only values (422)."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("admin_name", "admin_phone_number")
    @classmethod
    def _trim_admin_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _require_admin_pair(self) -> SocialGroupCreate:
        if (self.admin_name is None) != (self.admin_phone_number is None):
            raise ValueError(
                "admin_name and admin_phone_number must be provided together"
            )
        return self


class SocialGroupUpdate(BaseModel):
    """``PATCH /groups/{id}`` body.

    ``state`` and ``city`` can be corrected after creation and are validated
    against the same geo master data as ``POST /groups``. ``group_id`` stays
    immutable: it is the group's stable business identifier and may already be
    embedded in member IDs and external references.
    """

    name: str | None = None
    description: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str | None = None
    is_active: bool | None = None
    location_id: UUID | None = None
    state: str | None = None
    city: str | None = None

    @field_validator("state", "city")
    @classmethod
    def _reject_blank(cls, value: str | None) -> str | None:
        """Trim optional place fields, refusing explicit blanks (422)."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class SocialGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    group_id: str | None = None
    name: str
    state: str | None = None
    city: str | None = None
    description: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str
    is_active: bool
    location_id: UUID | None = None
    # Login accounts assigned to administer this group. Empty for a group that has
    # no Group Admin yet, which is a valid state a SUPER_ADMIN must be able to see.
    admins: list[GroupAdminOut] = []
    created_at: datetime
    updated_at: datetime


class GroupAdminOut(BaseModel):
    """A login account that administers a group."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    email: str | None = None
    phone_number: str | None = None
    is_active: bool


class GroupAdminAssignment(BaseModel):
    """Request body for ``POST /groups/{id}/admins``.

    ``role_name`` defaults to GROUP_ADMIN. A SUPER_ADMIN may also pass SUPER_ADMIN
    to grant a second platform administrator through the same endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_name: str = "GROUP_ADMIN"


class GroupMemberOut(BaseModel):
    """A member of a group, as returned by ``GET /groups/{id}/members``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID | None = None
    user_id: UUID | None = None
    full_name: str | None = None
    first_name: str
    last_name: str
    phone_number: str | None = None
    email: str | None = None
    membership_number: str | None = None
    is_primary: bool = False
    role: str = "MEMBER"
    role_name: str | None = None
    membership_status: str
    status: str | None = None
    group_id: UUID | None = None
    group_ids: list[UUID] = []
    # All groups the member belongs to (primary + secondary), each with id & name.
    groups: list[ProfileGroupItem] = []
    group_memberships: list[MemberGroupMembershipOut] = []
    group_name: str | None = None
    social_group_name: str | None = None
    social_group_association_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    created_at: datetime | None = None


class GroupMemberAssignment(BaseModel):
    """Request body for ``POST /groups/{id}/members``.

    Either ``member_id`` (attach an existing member) or ``user_id`` (promote a
    login account to a member of this group) must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    member_id: UUID | None = None
    user_id: UUID | None = None

    def model_post_init(self, __context) -> None:
        if self.member_id is None and self.user_id is None:
            raise ValueError("Either member_id or user_id is required")
        if self.member_id is not None and self.user_id is not None:
            raise ValueError("Supply member_id or user_id, not both")
