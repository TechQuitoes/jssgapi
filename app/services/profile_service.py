from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AuditAction
from app.core.exceptions import (
    AlreadyExistsException,
    ProfileNotFoundException,
    ValidationException,
)
from app.models.family import FamilyMember
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.professional import ProfessionalInformation
from app.models.user import User
from app.schemas.profile import ProfileGroupItem, ProfileOut
from app.services.audit_service import AuditService
from app.services.media_service import MediaService
from app.utils.helpers import format_to_dd_mm_yyyy, parse_any_date
from app.utils.validators import normalize_phone_number

DATE_PROFILE_FIELDS = frozenset(
    {
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
    }
)

PROFILE_FIELDS = (
    "full_name",
    "member_dob",
    "member_photo_link",
    "spouse_name",
    "spouse_mobile",
    "spouse_dob",
    "spouse_photo_link",
    "spouse_education",
    "spouse_occupation",
    "anniversary_date",
    "family_member_count",
    "son_name",
    "son_dob",
    "son_education",
    "son_occupation",
    "daughter_name",
    "daughter_dob",
    "daughter_education",
    "daughter_occupation",
    "unmarried_son1_details",
    "unmarried_son1_dob",
    "unmarried_son1_education",
    "unmarried_son1_occupation",
    "unmarried_son2_details",
    "unmarried_son2_dob",
    "unmarried_son2_education",
    "unmarried_son2_occupation",
    "unmarried_son3_details",
    "unmarried_son3_dob",
    "unmarried_son3_education",
    "unmarried_son3_occupation",
    "unmarried_daughter1_details",
    "unmarried_daughter1_dob",
    "unmarried_daughter1_education",
    "unmarried_daughter1_occupation",
    "unmarried_daughter2_details",
    "unmarried_daughter2_dob",
    "unmarried_daughter2_education",
    "unmarried_daughter2_occupation",
    "unmarried_daughter3_details",
    "unmarried_daughter3_dob",
    "unmarried_daughter3_education",
    "unmarried_daughter3_occupation",
    "member_education",
    "member_occupation",
    "company_name",
    "group_designation",
    "social_group_name",
    "social_group_association_name",
    "interest_fields",
    "business_address",
    "business_designation",
    "address",
    "city",
    "area",
    "phone_number",
    "email",
)

#: The first unmarried son/daughter is also kept under the shorter legacy names
#: the family records and older clients use, so both spellings of the same slot
#: always hold the same values.
PROFILE_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("unmarried_son1_details", "son_name"),
    ("unmarried_son1_dob", "son_dob"),
    ("unmarried_son1_education", "son_education"),
    ("unmarried_son1_occupation", "son_occupation"),
    ("unmarried_daughter1_details", "daughter_name"),
    ("unmarried_daughter1_dob", "daughter_dob"),
    ("unmarried_daughter1_education", "daughter_education"),
    ("unmarried_daughter1_occupation", "daughter_occupation"),
    ("social_group_association_name", "social_group_name"),
)


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def is_trustee_designation(value: str | None) -> bool:
    """Whether a group designation is a real (non-blank) value.

    ``null``, an empty string and a blank string all mean "no designation".
    Being a trustee is decided by the member's linked account role, not by this
    field; the helper is kept for callers that only need the blank check. The
    stored value is trimmed before the test, because sheet imports and free-text
    edits keep their padding.
    """
    return bool(value and value.strip())


def _normalize_values(values: dict[str, Any]) -> dict[str, str | None]:
    """Store profile values as strings, with both names of a shared slot set.

    ``profile_data`` is read back as strings by the profile view, so an int
    count or a date object must be converted before it is written. The son 1 /
    daughter 1 pairs are mirrored here so a write through either spelling keeps
    the two in step - the family records read the legacy spelling.
    """
    normalized = {key: _as_string(value) for key, value in values.items()}
    for canonical, legacy in PROFILE_FIELD_PAIRS:
        for source, target in ((canonical, legacy), (legacy, canonical)):
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
    return normalized


def _parse_date(value: str | None) -> date | None:
    return parse_any_date(value)


def _set_string_value(
    target: Any, attribute: str, value: str | None, max_length: int | None = None
) -> None:
    if max_length is not None and value is not None and len(value) > max_length:
        return
    setattr(target, attribute, value)


class ProfileService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditService(session)

    async def _get_member(self, user_id: UUID) -> Member | None:
        stmt = (
            select(Member)
            .options(
                selectinload(Member.user),
                selectinload(Member.group),
                selectinload(Member.location),
                selectinload(Member.family_members),
                selectinload(Member.professional_info),
                selectinload(Member.group_memberships).selectinload(
                    MemberGroupMembership.group
                ),
            )
            .where(
                (Member.user_id == user_id) | (Member.id == user_id),
                Member.is_deleted.is_(False),
            )
            .order_by(case((Member.user_id == user_id, 1), else_=2))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        member = result.scalar_one_or_none()
        if member is not None:
            return member

        # Fallback: check by user's phone number if member.user_id was not yet linked
        user = await self.session.get(User, user_id)
        if user and user.phone_number:
            normalized = user.phone_number.strip()
            local = normalized.removeprefix("+91")
            conds = [Member.contact_phone == normalized]
            if local != normalized:
                conds.append(Member.contact_phone == local)
            phone_stmt = (
                select(Member)
                .options(
                    selectinload(Member.user),
                    selectinload(Member.group),
                    selectinload(Member.location),
                    selectinload(Member.family_members),
                    selectinload(Member.professional_info),
                    selectinload(Member.group_memberships).selectinload(
                        MemberGroupMembership.group
                    ),
                )
                .where(
                    or_(*conds),
                    Member.is_deleted.is_(False),
                )
                .limit(1)
            )
            phone_res = await self.session.execute(phone_stmt)
            member = phone_res.scalar_one_or_none()
            if member is not None:
                if member.user_id is None:
                    member.user_id = user_id
                    await self.session.flush()
                return member
        return None

    async def get_profile(self, user_id: UUID) -> ProfileOut:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()
        return self._build_profile(member)

    async def update_profile(
        self,
        user_id: UUID,
        values: dict[str, str | None],
    ) -> ProfileOut:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()

        user = member.user
        if user is None and member.user_id:
            user = await self.session.get(User, member.user_id)
        if user is None:
            user = await self.session.get(User, user_id)

        if "phone_number" in values:
            raw_phone = values["phone_number"]
            if raw_phone is not None and str(raw_phone).strip():
                try:
                    normalized_phone = normalize_phone_number(str(raw_phone))
                except ValidationException:
                    normalized_phone = None

                if normalized_phone:
                    local_phone = normalized_phone.removeprefix("+91")

                    stmt = select(User).where(
                        User.is_deleted.is_(False),
                        (User.phone_number == normalized_phone)
                        | (User.phone_number == local_phone),
                    )
                    if user is not None:
                        stmt = stmt.where(User.id != user.id)
                    elif member.user_id is not None:
                        stmt = stmt.where(User.id != member.user_id)
                    else:
                        stmt = stmt.where(User.id != user_id)

                    existing = (await self.session.execute(stmt)).scalar_one_or_none()
                    if existing is not None:
                        raise AlreadyExistsException(
                            "This phone number is already registered."
                        )

                    if user is not None:
                        if user.phone_number not in (normalized_phone, local_phone):
                            user.phone_number = normalized_phone
                            user.firebase_uid = None
                elif len(str(raw_phone)) <= 20 and user is not None:
                    raise ValidationException("Invalid Indian phone number")
            elif raw_phone is not None and not str(raw_phone).strip():
                if user is not None:
                    raise ValidationException("Phone number cannot be empty")

        if "email" in values:
            raw_email = values["email"]
            if raw_email is not None and str(raw_email).strip():
                clean_email = str(raw_email).strip().lower()
                if len(clean_email) <= 255:
                    stmt = select(User).where(
                        User.is_deleted.is_(False),
                        User.email == clean_email,
                    )
                    if user is not None:
                        stmt = stmt.where(User.id != user.id)
                    elif member.user_id is not None:
                        stmt = stmt.where(User.id != member.user_id)
                    else:
                        stmt = stmt.where(User.id != user_id)

                    existing_email = (
                        await self.session.execute(stmt)
                    ).scalar_one_or_none()
                    if existing_email is not None:
                        raise AlreadyExistsException(
                            "This email is already registered."
                        )
                    if user is not None:
                        user.email = clean_email
            elif user is not None:
                user.email = None

        await self.apply_values(member, values)
        await self.audit.log(
            AuditAction.MEMBER_UPDATE,
            user_id=user_id,
            entity_type="member",
            entity_id=member.id,
            details={"fields": sorted(values)},
        )
        return self._build_profile(member)

    async def apply_values(self, member: Member, values: dict[str, str | None]) -> None:
        """Persist profile ``values`` on ``member``.

        Mirrors the values into ``members.profile_data`` and into the member
        columns, family and professional records they belong to. Callers must
        have ``member.group``/``member.location`` loaded only when they need the
        built profile back.
        """
        profile_data = dict(member.profile_data or {})
        values = _normalize_values(values)
        profile_data.update(values)
        member.profile_data = profile_data
        self._apply_member_fields(member, values)
        await self._sync_family_fields(member, values)
        await self._sync_professional_fields(member, values)
        await self.session.flush()

    async def upload_photo(
        self,
        user_id: UUID,
        *,
        file_name: str,
        content: bytes,
        mime_type: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()
        media = await MediaService(self.session).upload(
            owner_type="member",
            owner_id=member.id,
            file_name=file_name,
            content=content,
            mime_type=mime_type,
            is_public=True,
            uploaded_by=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
        )
        member.profile_photo_url = media.url
        profile_data = dict(member.profile_data or {})
        profile_data["member_photo_link"] = media.url
        member.profile_data = profile_data
        await self.session.flush()
        return media.url

    def build_profile(self, member: Member) -> ProfileOut:
        """Build the profile view for an already-loaded member.

        ``member.group`` and ``member.location`` must be eager loaded; they use
        the default lazy strategy and cannot be loaded inside the async session.
        """
        return self._build_profile(member)

    def get_profile_dict(self, member: Member) -> dict[str, str | None]:
        """Return the raw profile values dict, avoiding intermediate model instantiation."""
        values = self._fallback_values(member)
        profile_data = member.profile_data or {}
        for field, value in profile_data.items():
            if field in PROFILE_FIELDS:
                values[field] = _as_string(value)
        for canonical, legacy in PROFILE_FIELD_PAIRS:
            for source in (canonical, legacy):
                stored = _as_string(profile_data.get(source))
                if stored is not None:
                    values[canonical] = stored
                    values[legacy] = stored
                    break
        for date_field in DATE_PROFILE_FIELDS:
            if values.get(date_field):
                values[date_field] = format_to_dd_mm_yyyy(values[date_field])
        return values

    def get_member_groups_info(
        self, member: Member
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        primary_info: dict[str, Any] = {}

        memberships = getattr(member, "group_memberships", None) or []
        sorted_memberships = sorted(memberships, key=lambda item: item.priority)

        for m in sorted_memberships:
            grp = m.group
            if grp is not None and getattr(grp, "is_deleted", False):
                continue
            gid = str(m.group_id)
            gcode = grp.group_id if grp and grp.group_id else None
            gname = (grp.name if grp else None) or m.group_name or ""
            mnum = m.membership_number or (member.membership_number if m.is_primary else None)
            item = {
                "group_id": gid,
                "group_code": gcode,
                "group_name": gname,
                "membership_number": mnum,
                "is_primary": m.is_primary,
            }
            groups.append(item)
            if m.is_primary and not primary_info:
                primary_info = {
                    "group_id": gid,
                    "group_code": gcode,
                    "group_name": gname,
                    "membership_number": mnum,
                }

        if not groups and member.group and not getattr(member.group, "is_deleted", False):
            gid = str(member.group.id)
            gcode = member.group.group_id
            gname = member.group.name
            mnum = member.membership_number
            groups.append({
                "group_id": gid,
                "group_code": gcode,
                "group_name": gname,
                "membership_number": mnum,
                "is_primary": True,
            })
            primary_info = {
                "group_id": gid,
                "group_code": gcode,
                "group_name": gname,
                "membership_number": mnum,
            }

        if groups and not primary_info:
            primary_info = {
                "group_id": groups[0]["group_id"],
                "group_name": groups[0]["group_name"],
                "membership_number": groups[0]["membership_number"],
            }

        if not primary_info.get("membership_number") and member.membership_number:
            primary_info["membership_number"] = member.membership_number

        return groups, primary_info

    def _build_profile(self, member: Member) -> ProfileOut:
        values = self.get_profile_dict(member)
        groups, primary_info = self.get_member_groups_info(member)
        values["groups"] = groups
        if primary_info.get("group_id"):
            values["group_id"] = primary_info["group_id"]
        if primary_info.get("group_name"):
            values["group_name"] = primary_info["group_name"]
            values["social_group_name"] = primary_info["group_name"]
            values["social_group_association_name"] = primary_info["group_name"]
        if primary_info.get("membership_number"):
            values["membership_number"] = primary_info["membership_number"]

        memberships = getattr(member, "group_memberships", None) or []
        sorted_memberships = sorted(memberships, key=lambda item: item.priority)
        group_memberships: list[dict[str, Any]] = []
        for m in sorted_memberships:
            grp = m.group
            if grp is not None and getattr(grp, "is_deleted", False):
                continue
            mnum = m.membership_number or (member.membership_number if m.is_primary else None)
            group_memberships.append({
                "id": m.id,
                "group_id": m.group_id,
                "group_name": m.group_name,
                "group_code": m.group_code,
                "membership_number": mnum,
                "priority": m.priority,
                "is_primary": m.is_primary,
                "joined_at": m.joined_at,
            })

        if not group_memberships and member.group and not getattr(member.group, "is_deleted", False):
            group_memberships.append({
                "id": member.id,
                "group_id": member.group.id,
                "group_name": member.group.name,
                "group_code": member.group.group_id,
                "membership_number": member.membership_number,
                "priority": 1,
                "is_primary": True,
                "joined_at": member.joined_at,
            })

        values["group_memberships"] = group_memberships
        return ProfileOut(**values)

    def _fallback_values(self, member: Member) -> dict[str, str | None]:
        user = member.user
        spouse = self._family_member(member, {"spouse", "wife", "husband"})
        son = self._family_member(member, {"son"})
        daughter = self._family_member(member, {"daughter"})
        professional = self._professional_info(member)
        location = member.location
        if location is not None and location.is_deleted:
            location = None
        group = member.group
        if group is not None and group.is_deleted:
            group = None

        full_name = user.full_name if user is not None else None
        if not full_name:
            full_name = " ".join(
                part
                for part in (member.first_name, member.middle_name, member.last_name)
                if part
            )

        return {
            "full_name": _as_string(full_name),
            "member_dob": format_to_dd_mm_yyyy(member.date_of_birth),
            "member_photo_link": member.profile_photo_url,
            "spouse_name": spouse.name if spouse else None,
            "spouse_mobile": spouse.contact_phone if spouse else None,
            "spouse_dob": format_to_dd_mm_yyyy(spouse.date_of_birth) if spouse else None,
            "spouse_photo_link": None,
            "spouse_education": None,
            "spouse_occupation": spouse.occupation if spouse else None,
            "anniversary_date": None,
            "family_member_count": str(len(member.family_members)),
            "son_name": son.name if son else None,
            "son_dob": format_to_dd_mm_yyyy(son.date_of_birth) if son else None,
            "son_education": None,
            "son_occupation": son.occupation if son else None,
            "daughter_name": daughter.name if daughter else None,
            "daughter_dob": format_to_dd_mm_yyyy(daughter.date_of_birth) if daughter else None,
            "daughter_education": None,
            "daughter_occupation": daughter.occupation if daughter else None,
            "member_education": None,
            "member_occupation": member.occupation_summary,
            "company_name": professional.company_name if professional else None,
            "group_designation": professional.designation if professional else None,
            "social_group_name": group.name if group else None,
            "social_group_association_name": group.name if group else None,
            "interest_fields": None,
            "business_designation": None,
            "address": member.address_line,
            "city": location.city if location else None,
            "area": location.area if location else None,
            "phone_number": member.contact_phone
            or (user.phone_number if user else None),
            "email": member.contact_email or (user.email if user else None),
        }

    @staticmethod
    def _family_member(member: Member, aliases: set[str]) -> FamilyMember | None:
        for item in sorted(
            member.family_members,
            key=lambda value: value.created_at.isoformat() if value.created_at else "",
        ):
            if item.relationship_type.strip().casefold() in aliases:
                return item
        return None

    @staticmethod
    def _professional_info(member: Member) -> ProfessionalInformation | None:
        if not member.professional_info:
            return None
        return sorted(
            member.professional_info,
            key=lambda value: value.created_at.isoformat() if value.created_at else "",
        )[0]

    def _apply_member_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        if "full_name" in values and values["full_name"] is not None and member.user:
            _set_string_value(member.user, "full_name", values["full_name"], 255)
        if "member_dob" in values:
            if values["member_dob"] is None:
                member.date_of_birth = None
            else:
                parsed = _parse_date(values["member_dob"])
                if parsed is not None:
                    member.date_of_birth = parsed
        if "member_photo_link" in values:
            member.profile_photo_url = values["member_photo_link"]
        if "member_occupation" in values:
            member.occupation_summary = values["member_occupation"]
        if "address" in values:
            member.address_line = values["address"]
        if "phone_number" in values:
            _set_string_value(member, "contact_phone", values["phone_number"], 20)
        if "email" in values:
            _set_string_value(member, "contact_email", values["email"], 255)

    async def _sync_family_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        definitions = (
            ("spouse", {"spouse", "wife", "husband"}, "spouse"),
            ("son", {"son"}, "son"),
            ("daughter", {"daughter"}, "daughter"),
        )
        for relationship, aliases, prefix in definitions:
            target = self._family_member(member, aliases)
            name_key = f"{prefix}_name"
            name = values.get(name_key)
            if name is not None:
                if target is None:
                    if len(name) <= 255:
                        target = FamilyMember(
                            member_id=member.id,
                            name=name,
                            relationship_type=relationship,
                        )
                        self.session.add(target)
                        member.family_members.append(target)
                else:
                    _set_string_value(target, "name", name, 255)
            if target is None:
                continue
            for suffix, attribute in (
                ("dob", "date_of_birth"),
                ("occupation", "occupation"),
                ("mobile", "contact_phone"),
            ):
                key = f"{prefix}_{suffix}"
                if key not in values:
                    continue
                value = values[key]
                if attribute == "date_of_birth":
                    if value is None:
                        target.date_of_birth = None
                    else:
                        parsed = _parse_date(value)
                        if parsed is not None:
                            target.date_of_birth = parsed
                else:
                    _set_string_value(
                        target,
                        attribute,
                        value,
                        20 if attribute == "contact_phone" else 255,
                    )

        extra_children = (
            ("son", "unmarried_son2"),
            ("son", "unmarried_son3"),
            ("daughter", "unmarried_daughter2"),
            ("daughter", "unmarried_daughter3"),
        )
        for rel_type, prefix in extra_children:
            child_name = values.get(f"{prefix}_details")
            if not child_name or len(child_name) > 255:
                continue
            target = next(
                (
                    f
                    for f in member.family_members
                    if f.relationship_type.strip().casefold() == rel_type
                    and f.name.strip().casefold() == child_name.strip().casefold()
                ),
                None,
            )
            if target is None:
                target = FamilyMember(
                    member_id=member.id,
                    name=child_name,
                    relationship_type=rel_type,
                )
                self.session.add(target)
                member.family_members.append(target)

            child_dob = values.get(f"{prefix}_dob")
            if child_dob:
                parsed_dob = _parse_date(child_dob)
                if parsed_dob is not None:
                    target.date_of_birth = parsed_dob
            child_occ = values.get(f"{prefix}_occupation")
            if child_occ:
                _set_string_value(target, "occupation", child_occ, 255)

    async def _sync_professional_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        mapping = {
            "member_occupation": "occupation",
            "company_name": "company_name",
            "group_designation": "designation",
        }
        if not any(key in values for key in mapping):
            return
        target = self._professional_info(member)
        if target is None and any(values.get(key) is not None for key in mapping):
            target = ProfessionalInformation(
                member_id=member.id,
                is_visible_in_directory=True,
            )
            self.session.add(target)
            member.professional_info.append(target)
        if target is None:
            return
        for key, attribute in mapping.items():
            if key in values:
                _set_string_value(target, attribute, values[key], 255)
