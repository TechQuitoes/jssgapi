from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

from app.core.constants import MemberStatus
from app.schemas.family import FamilyMemberOut
from app.schemas.professional import ProfessionalInfoOut
from app.schemas.profile import (
    MemberGroupMembershipOut,
    ProfileGroupItem,
    ProfileOut,
    ProfileUpdate,
)
from app.schemas.user import UserOut
from app.utils.helpers import format_to_dd_mm_yyyy


def _parse_date_of_birth(value: Any) -> date | None:
    """Accept a date or the date formats a member record arrives in.

    Admin panels send ISO dates, while spreadsheets and mobile forms send
    ``DD/MM/YYYY``; both land in the same column, so neither may be rejected.
    An empty string means "no date", not a validation error.
    """
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    # A timestamp's time part is irrelevant to a date of birth.
    text = text.split("T", 1)[0].split(" ", 1)[0]
    parts = text.replace("/", "-").split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        if len(parts[0]) == 4:  # YYYY-MM-DD / YYYY/MM/DD
            year, month, day = (int(part) for part in parts)
        else:  # DD-MM-YYYY / DD/MM/YYYY
            day, month, year = (int(part) for part in parts)
        try:
            return date(year, month, day)
        except ValueError:
            raise ValueError(
                "date_of_birth must be a real date in YYYY-MM-DD or DD/MM/YYYY"
            ) from None
    raise ValueError("date_of_birth must be a date in YYYY-MM-DD or DD/MM/YYYY")


#: A nullable ``date_of_birth`` that also accepts the text formats a member
#: record arrives in.
DateOfBirth = Annotated[date | None, BeforeValidator(_parse_date_of_birth)]


class MemberCreate(ProfileUpdate):
    """Create payload for ``POST /api/v1/members``.

    Accepts the ``members`` columns plus every profile field of
    :class:`ProfileUpdate`, so a member and their profile data are created in a
    single request. ``full_name`` and ``phone_number`` are required; the other
    profile fields are optional and stored in ``members.profile_data``.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: UUID | None = None
    group_id: UUID | None = None
    group_ids: list[UUID] | None = None
    location_id: UUID | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    gender: str | None = None
    date_of_birth: DateOfBirth = None
    blood_group: str | None = None
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None
    password: str | None = None

    full_name: str = Field(min_length=1)
    phone_number: str = Field(
        min_length=1,
        validation_alias=AliasChoices("phone_number", "mobile_number"),
    )

    def member_columns(self) -> dict[str, Any]:
        """Values for the ``members`` table, without the profile fields.

        Profile fields are excluded (they live in ``profile_data``) and so are
        the keys the client never sent, so a partial create cannot null out a
        column it did not mention.
        """
        return self.model_dump(
            exclude=set(ProfileUpdate.model_fields) | {"group_ids", "password"},
            exclude_none=True,
        )

    def requested_group_ids(self) -> list[UUID]:
        """Ordered selection, preferring ``group_ids`` over legacy ``group_id``."""
        source = self.group_ids
        if source is None:
            source = [self.group_id] if self.group_id is not None else []
        ordered: list[UUID] = []
        for group_id in source:
            if group_id not in ordered:
                ordered.append(group_id)
        return ordered

    def profile_values(self) -> dict[str, str | None]:
        """Profile fields supplied by the client (dates as ISO strings)."""
        return {
            name: getattr(self, name)
            for name in ProfileUpdate.model_fields
            if name in self.model_fields_set
        }


class MemberUpdate(ProfileUpdate):
    """Payload for ``PATCH /api/v1/members/{id}``.

    Accepts the ``members`` columns plus every editable profile field of
    :class:`ProfileUpdate`, so an admin can maintain a member's full profile in
    one request instead of a second round trip to ``PUT /profile`` (which writes
    only the caller's own profile).

    Keys the client sent are applied and keys it did not send are left alone.
    A profile field explicitly set to ``null`` clears that field; a member
    column set to ``null`` is ignored, because the project has never allowed a
    column to be cleared from a PATCH and a client sending a whole object back
    with empty columns must not erase them.
    """

    model_config = ConfigDict(extra="ignore")

    group_id: UUID | None = None
    group_ids: list[UUID] | None = None
    location_id: UUID | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    gender: str | None = None
    date_of_birth: DateOfBirth = None
    blood_group: str | None = None
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None
    password: str | None = None

    def member_columns(self) -> dict[str, Any]:
        """Only the fields the client actually sent, minus the profile fields.

        ``exclude_unset`` keeps an absent key from touching its column and
        ``exclude_none`` keeps an explicit null from clearing one, so a PATCH
        can only ever set values.
        """
        return self.model_dump(
            exclude=set(ProfileUpdate.model_fields) | {"group_ids", "password"},
            exclude_none=True,
            exclude_unset=True,
        )

    def requested_group_ids(self) -> list[UUID]:
        """Ordered selection, preferring ``group_ids`` over legacy ``group_id``."""
        source = self.group_ids
        if source is None:
            source = [self.group_id] if self.group_id is not None else []
        ordered: list[UUID] = []
        for group_id in source:
            if group_id not in ordered:
                ordered.append(group_id)
        return ordered

    def profile_values(self) -> dict[str, str | None]:
        """Profile fields supplied by the client (dates as ISO strings)."""
        return {
            name: getattr(self, name)
            for name in ProfileUpdate.model_fields
            if name in self.model_fields_set
        }


class MemberApproveRequest(BaseModel):
    status: MemberStatus
    rejection_reason: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID | None = None
    user_id: UUID | None = None
    group_id: str | UUID | None = None
    # The linked group's name (``group_id`` -> ``social_groups.name``), so a
    # client can render the group without a second request. Distinct from the
    # free-text ``social_group_name`` profile field the sheet import fills in.
    group_name: str | None = None
    group_code: str | None = None
    social_group_name: str | None = None
    social_group_association_name: str | None = None
    groups: list[ProfileGroupItem] = []
    group_memberships: list[MemberGroupMembershipOut] = []
    location_id: UUID | None = None
    first_name: str
    middle_name: str | None = None
    last_name: str
    gender: str | None = None
    date_of_birth: str | None = None
    blood_group: str | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _format_dob(cls, v: Any) -> str | None:
        return format_to_dd_mm_yyyy(v)
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None
    membership_status: str
    status: str | None = None
    joined_at: datetime | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    is_profile_complete: bool
    user: UserOut | None = None
    family_members: list[FamilyMemberOut] = []
    professional_info: list[ProfessionalInfoOut] = []
    created_at: datetime
    updated_at: datetime


class MemberListItemOut(MemberOut, ProfileOut):
    """List item for ``GET /api/v1/members``: the member record plus the
    profile fields stored in ``members.profile_data`` (or derived from the
    member's columns, family, professional and location relations)."""

    member_id: UUID | None = None
    status: str | None = None
