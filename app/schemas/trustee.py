from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TrusteeCreate(BaseModel):
    user_id: UUID | None = None
    # The member this seat belongs to. Optional so a trustee can still be recorded
    # for someone who is not a member of the directory.
    member_id: UUID | None = None
    group_id: UUID
    first_name: str
    last_name: str
    designation: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool = True
    is_current: bool = True
    sort_order: int = 0


class TrusteeUpdate(BaseModel):
    user_id: UUID | None = None
    member_id: UUID | None = None
    group_id: UUID | None = None
    first_name: str | None = None
    last_name: str | None = None
    designation: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool | None = None
    is_current: bool | None = None
    sort_order: int | None = None


class TrusteeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    member_id: UUID | None = None
    group_id: UUID | None = None
    first_name: str
    last_name: str
    designation: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool
    is_current: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class MemberTrusteeOut(BaseModel):
    """A post holder of the directory, derived from the member.

    A post holder is not a second record: it is the member whose linked login
    account carries ``GROUP_ADMIN``/``SUPER_ADMIN`` or whose group designation is
    a recognised post-holder office (President, Secretary, Treasurer, ...). The
    member's own id is therefore the id of this item, and ``member_id`` repeats
    it so a client can treat the post holder as a member without a second lookup.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID
    group_id: UUID | None = None
    group_name: str | None = None
    first_name: str
    last_name: str
    full_name: str
    #: The administrator tag: ``GROUP_ADMIN`` or ``SUPER_ADMIN``; null when the
    #: member qualifies only through a post-holder designation.
    role: str | None = None
    designation: str
    is_post_holder: bool = True
    post_holder_title: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    city: str | None = None
    membership_status: str
    membership_number: str | None = None
    joined_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
