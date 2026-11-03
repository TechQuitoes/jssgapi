from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.utils.helpers import format_to_dd_mm_yyyy

# Request aliases only: responses always use the canonical field name, so
# existing clients keep seeing the same keys.


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    member_dob: str | None = None
    member_photo_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("member_photo_link", "member_photo_url"),
    )
    spouse_name: str | None = None
    spouse_mobile: str | None = None
    spouse_dob: str | None = None
    spouse_photo_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("spouse_photo_link", "spouse_photo_url"),
    )
    spouse_education: str | None = None
    spouse_occupation: str | None = None
    anniversary_date: str | None = None
    family_member_count: int | str | None = None
    son_name: str | None = None
    son_dob: str | None = None
    son_education: str | None = None
    son_occupation: str | None = None
    daughter_name: str | None = None
    daughter_dob: str | None = None
    daughter_education: str | None = None
    daughter_occupation: str | None = None
    unmarried_son1_details: str | None = None
    unmarried_son1_dob: str | None = None
    unmarried_son1_education: str | None = None
    unmarried_son1_occupation: str | None = None
    unmarried_son2_details: str | None = None
    unmarried_son2_dob: str | None = None
    unmarried_son2_education: str | None = None
    unmarried_son2_occupation: str | None = None
    unmarried_son3_details: str | None = None
    unmarried_son3_dob: str | None = None
    unmarried_son3_education: str | None = None
    unmarried_son3_occupation: str | None = None
    unmarried_daughter1_details: str | None = None
    unmarried_daughter1_dob: str | None = None
    unmarried_daughter1_education: str | None = None
    unmarried_daughter1_occupation: str | None = None
    unmarried_daughter2_details: str | None = None
    unmarried_daughter2_dob: str | None = None
    unmarried_daughter2_education: str | None = None
    unmarried_daughter2_occupation: str | None = None
    unmarried_daughter3_details: str | None = None
    unmarried_daughter3_dob: str | None = None
    unmarried_daughter3_education: str | None = None
    unmarried_daughter3_occupation: str | None = None
    member_education: str | None = None
    member_occupation: str | None = None
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "company_business_name"),
    )
    group_designation: str | None = None
    social_group_name: str | None = None
    social_group_association_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "social_group_association_name",
            "social_group_name",
            "social_group",
            "Social Group / Association Name",
        ),
    )
    interest_fields: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "interest_fields", "interest_field_of_social_activities"
        ),
    )
    business_address: str | None = None
    business_designation: str | None = None
    address: str | None = None
    city: str | None = None
    area: str | None = None
    phone_number: str | None = Field(
        default=None,
        validation_alias=AliasChoices("phone_number", "mobile_number"),
    )
    email: str | None = None


class ProfileGroupItem(BaseModel):
    group_id: str | None = None
    group_code: str | None = None
    group_name: str | None = None
    membership_number: str | None = None
    is_primary: bool = False


class MemberGroupMembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    group_id: UUID
    group_name: str | None = None
    group_code: str | None = None
    membership_number: str | None = None
    priority: int
    is_primary: bool
    joined_at: datetime | None = None


class ProfileOut(ProfileUpdate):
    model_config = ConfigDict(extra="ignore")

    membership_number: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    groups: list[ProfileGroupItem] = []
    group_memberships: list[MemberGroupMembershipOut] = []

    @field_validator(
        "member_dob",
        "spouse_dob",
        "anniversary_date",
        "son_dob",
        "daughter_dob",
        "unmarried_son1_dob",
        "unmarried_son2_dob",
        "unmarried_son3_dob",
        "unmarried_daughter1_dob",
        "unmarried_daughter2_dob",
        "unmarried_daughter3_dob",
        mode="before",
    )
    @classmethod
    def _standardize_date(cls, v: Any) -> Any:
        return format_to_dd_mm_yyyy(v)


class ProfileResponse(BaseModel):
    success: bool = True
    profile: ProfileOut
    data: ProfileOut | None = None


class ProfilePhotoResponse(BaseModel):
    success: bool = True
    photo_link: str
