from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.constants import LEGACY_ROLE_ALIASES, RoleName
from app.models.family import FamilyMember
from app.models.group import SocialGroup
from app.models.location import Location
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.professional import ProfessionalInformation
from app.models.user import Role, User
from app.repositories.base_repository import BaseRepository
from app.utils.helpers import is_valid_uuid

# ``Member.group`` and ``Member.location`` use the default lazy strategy, which
# cannot be resolved inside an async session. Any query whose caller needs the
# profile fields (social_group_name, city, area) must eager load them.
MEMBER_PROFILE_LOADS = (
    selectinload(Member.group),
    selectinload(Member.group_memberships).selectinload(
        MemberGroupMembership.group
    ),
    selectinload(Member.location),
)

#: The roles whose holders are trustees of the group their member row belongs to.
TRUSTEE_ROLES = frozenset({RoleName.SUPER_ADMIN, RoleName.GROUP_ADMIN})

#: Every stored role label that resolves to a trustee role, including the
#: retired aliases that old imports may still carry. Matching the raw labels in
#: SQL keeps the filter working even when a row predates the role cleanup.
_TRUSTEE_ROLE_LABELS_BY_ROLE: dict[RoleName, frozenset[str]] = {
    role: frozenset(
        {role.value}
        | {
            label
            for label, resolved in LEGACY_ROLE_ALIASES.items()
            if resolved is role
        }
    )
    for role in TRUSTEE_ROLES
}
TRUSTEE_ROLE_LABELS = frozenset(
    label for labels in _TRUSTEE_ROLE_LABELS_BY_ROLE.values() for label in labels
)

#: Exact group designations that make a member a post holder even without a
#: ``GROUP_ADMIN``/``SUPER_ADMIN`` login role. Comparisons are case-insensitive
#: and trim surrounding whitespace, so an imported ``CHAIRMAN`` matches too.
#: Common punctuation aliases are listed so ``Vice-President`` and
#: ``Chairman and Managing Director`` are treated as the same office.
POST_HOLDER_DESIGNATIONS = frozenset(
    {
        "president",
        "vice president",
        "vice-president",
        "chairman",
        "chairman & managing director",
        "chairman and managing director",
        "protector",
        "secretary",
        "treasurer",
        "publicity secretary",
        "secretary treasurer",
        "secretary/treasurer",
    }
)


def _normalized_designation(column):
    """A designation column folded to trimmed, single-spaced lowercase text."""
    return func.lower(
        func.regexp_replace(func.btrim(column, _TRIM_CHARS), r"\s+", " ", "g")
    )


def post_holder_designation_filter():
    """Members whose group designation is one of the post-holder offices.

    The designation lives in ``members.profile_data`` and is mirrored into
    ``professional_information.designation``; either store may hold it. Exact
    matching keeps unrelated job titles such as ``Company Secretary`` or
    ``Past President`` out of the post-holder listing.
    """
    stored = _normalized_designation(
        Member.profile_data["group_designation"].astext
    )
    mirrored = (
        select(ProfessionalInformation.member_id)
        .where(
            ProfessionalInformation.member_id == Member.id,
            _normalized_designation(ProfessionalInformation.designation).in_(
                POST_HOLDER_DESIGNATIONS
            ),
        )
        .exists()
    )
    return or_(stored.in_(POST_HOLDER_DESIGNATIONS), mirrored)


def trustee_role_filter(role: RoleName | None = None):
    """Members who are post holders of their group.

    With no explicit role, a member qualifies when their account carries
    ``GROUP_ADMIN`` or ``SUPER_ADMIN`` **or** their group designation is one of
    :data:`POST_HOLDER_DESIGNATIONS`. Passing a role narrows the result to that
    administrator role only, so a designation-only post holder is not returned.

    The member is not a second record. The member's own ``group_id`` decides
    which group's post holders they are, so a SUPER_ADMIN is a post holder of
    the group they were added to rather than of every group.
    """
    labels = (
        _TRUSTEE_ROLE_LABELS_BY_ROLE[role] if role is not None else TRUSTEE_ROLE_LABELS
    )
    role_condition = (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .where(
            User.id == Member.user_id,
            User.is_deleted.is_(False),
            func.upper(func.btrim(Role.name, _TRIM_CHARS)).in_(labels),
        )
        .exists()
    )
    if role is not None:
        return role_condition
    return or_(role_condition, post_holder_designation_filter())


def member_group_filter(group_id):
    """Members linked to a group through any membership, not just the primary.

    The stale ``members.group_id`` pointer is also accepted so a row that
    predates the backfill is never hidden while the migration catches up.
    """
    linked = select(MemberGroupMembership.id).where(
        MemberGroupMembership.member_id == Member.id,
        MemberGroupMembership.group_id == group_id,
    ).exists()
    return or_(Member.group_id == group_id, linked)


def group_name_filter(group_name: str):
    """Case-insensitive match of a member's social group by name.

    A member's group name lives in one of two places: the free-text
    ``social_group_name`` kept in ``members.profile_data`` (sheet imports and
    members never linked to a group), or the linked ``social_groups`` row when
    ``group_id`` is set. Either may match, so the filter can never hide a member
    whose name is stored on the other side.
    """
    needle = group_name.strip().casefold()
    # Stored names may carry stray padding (sheet imports and free-text edits do
    # not trim), while the filter value comes from the trimmed option list, so
    # both sides are trimmed before the case-insensitive comparison.
    profile_name = func.lower(
        func.trim(Member.profile_data["social_group_name"].astext)
    )
    linked_group = (
        select(MemberGroupMembership.id)
        .join(SocialGroup, SocialGroup.id == MemberGroupMembership.group_id)
        .where(
            MemberGroupMembership.member_id == Member.id,
            SocialGroup.is_deleted.is_(False),
            func.lower(func.trim(SocialGroup.name)) == needle,
        )
        .exists()
    )
    legacy_linked = (
        select(SocialGroup.id)
        .where(
            SocialGroup.id == Member.group_id,
            SocialGroup.is_deleted.is_(False),
            func.lower(func.trim(SocialGroup.name)) == needle,
        )
        .exists()
    )
    return or_(profile_name == needle, linked_group, legacy_linked)


#: The characters trimmed off a stored designation. ``btrim`` only strips
#: spaces by default, so the whitespace a sheet import may leave behind is
#: listed explicitly.
_TRIM_CHARS = " \t\n\r"


def designation_filter(designation: str | None = None):
    """Members that carry a group designation, optionally a specific one.

    The designation is kept in ``members.profile_data`` and mirrored into
    ``professional_information.designation``; either store may hold it, so both
    are read. A blank value (``null``, ``""`` or whitespace only) means the
    member has no designation. Being a post holder is decided by the linked
    account's role or by an exact post-holder designation (see
    :func:`trustee_role_filter`); this filter only narrows by a designation.
    """
    stored = func.btrim(Member.profile_data["group_designation"].astext, _TRIM_CHARS)
    mirrored = (
        select(ProfessionalInformation.member_id)
        .where(
            ProfessionalInformation.member_id == Member.id,
            func.btrim(ProfessionalInformation.designation, _TRIM_CHARS) != "",
        )
        .exists()
    )
    condition = or_(stored != "", mirrored)
    needle = (designation or "").strip()
    if not needle:
        return condition
    # The designation is free text, so the match is case-insensitive and the
    # padded stored value is trimmed on both sides before comparing.
    return and_(
        condition,
        or_(
            func.lower(stored) == needle.casefold(),
            Member.id.in_(
                select(ProfessionalInformation.member_id).where(
                    func.lower(
                        func.btrim(ProfessionalInformation.designation, _TRIM_CHARS)
                    )
                    == needle.casefold()
                )
            ),
        ),
    )


def _build_member_search_expressions(query: str):
    clean_q = query.strip()
    if not clean_q:
        return None, None

    prefix = f"{clean_q}%"
    contains = f"%{clean_q}%"
    tokens = [t for t in clean_q.split() if t]

    full_name_first_last = func.concat(
        Member.first_name, " ", func.coalesce(Member.last_name, "")
    )
    full_name_with_middle = func.concat(
        Member.first_name,
        " ",
        func.coalesce(Member.middle_name, ""),
        " ",
        func.coalesce(Member.last_name, ""),
    )
    full_name_last_first = func.concat(
        Member.last_name, " ", func.coalesce(Member.first_name, "")
    )

    spouse_rel_types = ["spouse", "wife", "husband"]
    spouse_name_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.name.ilike(contains),
        )
        .exists()
    )
    spouse_name_prefix_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.name.ilike(prefix),
        )
        .exists()
    )
    spouse_phone_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.contact_phone.ilike(contains),
        )
        .exists()
    )

    single_field_conditions = [
        Member.first_name.ilike(contains),
        Member.middle_name.ilike(contains),
        Member.last_name.ilike(contains),
        full_name_first_last.ilike(contains),
        full_name_with_middle.ilike(contains),
        full_name_last_first.ilike(contains),
        Member.profile_data["full_name"].astext.ilike(contains),
        Member.contact_email.ilike(contains),
        Member.contact_phone.ilike(contains),
        Member.membership_number.ilike(contains),
        Member.address_line.ilike(contains),
        Member.occupation_summary.ilike(contains),
        # Spouse fields
        Member.profile_data["spouse_name"].astext.ilike(contains),
        Member.profile_data["spouse_mobile"].astext.ilike(contains),
        spouse_name_exists,
        spouse_phone_exists,
        # Location fields (City, State, Area, Name)
        Location.name.ilike(contains),
        Location.city.ilike(contains),
        Location.state.ilike(contains),
        Location.area.ilike(contains),
        Member.profile_data["city"].astext.ilike(contains),
        Member.profile_data["state"].astext.ilike(contains),
        Member.profile_data["area"].astext.ilike(contains),
        Member.profile_data["address"].astext.ilike(contains),
        # Social Group
        SocialGroup.name.ilike(contains),
        Member.profile_data["social_group_name"].astext.ilike(contains),
        # Occupation / Education
        Member.profile_data["member_occupation"].astext.ilike(contains),
        Member.profile_data["member_education"].astext.ilike(contains),
        Member.profile_data["company_name"].astext.ilike(contains),
        # Member ID & Group ID prefix / contains
        cast(Member.id, String).ilike(f"{clean_q}%"),
        cast(Member.group_id, String).ilike(f"{clean_q}%"),
    ]

    if is_valid_uuid(clean_q):
        parsed_uuid = UUID(clean_q)
        single_field_conditions.extend([
            Member.id == parsed_uuid,
            Member.group_id == parsed_uuid,
        ])

    single_needle_match = or_(*single_field_conditions)

    if len(tokens) > 1:
        token_clauses = []
        for t in tokens:
            t_contains = f"%{t}%"
            t_spouse_exists = (
                select(FamilyMember.id)
                .where(
                    FamilyMember.member_id == Member.id,
                    func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
                    FamilyMember.name.ilike(t_contains),
                )
                .exists()
            )
            t_cond = or_(
                Member.first_name.ilike(t_contains),
                Member.middle_name.ilike(t_contains),
                Member.last_name.ilike(t_contains),
                Member.profile_data["full_name"].astext.ilike(t_contains),
                Member.profile_data["spouse_name"].astext.ilike(t_contains),
                t_spouse_exists,
                Location.city.ilike(t_contains),
                Location.state.ilike(t_contains),
                Location.name.ilike(t_contains),
                Member.profile_data["city"].astext.ilike(t_contains),
                Member.profile_data["state"].astext.ilike(t_contains),
                SocialGroup.name.ilike(t_contains),
                Member.profile_data["social_group_name"].astext.ilike(t_contains),
                Member.contact_phone.ilike(t_contains),
                Member.membership_number.ilike(t_contains),
                Member.contact_email.ilike(t_contains),
            )
            token_clauses.append(t_cond)
        where_condition = or_(single_needle_match, and_(*token_clauses))
    else:
        where_condition = single_needle_match

    rank_expr = case(
        (Member.first_name.ilike(clean_q), 1),
        (Member.first_name.ilike(prefix), 2),
        (Member.last_name.ilike(prefix), 3),
        (full_name_first_last.ilike(prefix), 4),
        (Member.profile_data["spouse_name"].astext.ilike(prefix), 5),
        (spouse_name_prefix_exists, 5),
        (Location.city.ilike(prefix), 6),
        (Member.profile_data["city"].astext.ilike(prefix), 6),
        (Location.state.ilike(prefix), 7),
        (Member.profile_data["state"].astext.ilike(prefix), 7),
        (SocialGroup.name.ilike(prefix), 8),
        (Member.profile_data["social_group_name"].astext.ilike(prefix), 8),
        (Member.first_name.ilike(contains), 9),
        (Member.last_name.ilike(contains), 10),
        (full_name_first_last.ilike(contains), 11),
        (Member.profile_data["spouse_name"].astext.ilike(contains), 12),
        (spouse_name_exists, 12),
        (Location.city.ilike(contains), 13),
        (Member.profile_data["city"].astext.ilike(contains), 13),
        (Location.state.ilike(contains), 14),
        (SocialGroup.name.ilike(contains), 15),
        (Member.membership_number.ilike(prefix), 16),
        (Member.contact_phone.ilike(prefix), 17),
        (Member.membership_number.ilike(contains), 18),
        (Member.contact_phone.ilike(contains), 19),
        else_=30,
    )

    return where_condition, rank_expr


class MemberRepository(BaseRepository[Member]):
    model = Member

    def apply_filters(self, stmt, filters: dict[str, object]):
        for key, value in filters.items():
            if value is None:
                continue
            if key == "group_id":
                stmt = stmt.where(member_group_filter(value))
            else:
                stmt = stmt.where(getattr(Member, key) == value)
        return stmt

    async def get_by_id(self, instance_id) -> Member | None:
        """Fetch a member that is not soft-deleted.

        ``DELETE /api/v1/members/{id}`` only flips ``is_deleted``, so a lookup
        that ignores the flag would keep serving a member that was deleted,
        and every write endpoint would still accept it.
        """
        result = await self.session.execute(
            select(Member).where(Member.id == instance_id, Member.is_deleted.is_(False))
        )
        return result.scalar_one_or_none()

    async def get_by_user_id(self, user_id) -> Member | None:
        result = await self.session.execute(
            select(Member).where(Member.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_membership_number(self, number: str) -> Member | None:
        result = await self.session.execute(
            select(Member).where(Member.membership_number == number)
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Member | None:
        normalized = phone.strip()
        local = normalized.removeprefix("+91")
        conditions = [Member.contact_phone == normalized]
        if local != normalized:
            conditions.append(Member.contact_phone == local)
        result = await self.session.execute(
            select(Member).where(
                Member.is_deleted.is_(False),
                or_(*conditions),
            )
        )
        return result.scalars().first()

    @staticmethod
    def _phone_candidates(phone: str) -> list[str]:
        raw = phone.strip().replace(" ", "").replace("-", "")
        local = raw.removeprefix("+91")
        if local.startswith("0") and len(local) > 10:
            local = local[1:]
        if len(local) == 10 and local.isdigit():
            with_prefix = f"+91{local}"
            return list({raw, local, with_prefix})
        return list({raw, local})

    async def get_by_spouse_phone(self, phone: str) -> Member | None:
        candidates = self._phone_candidates(phone)
        spouse_rel_types = ["spouse", "wife", "husband"]
        spouse_phone_match = (
            select(FamilyMember.id)
            .where(
                FamilyMember.member_id == Member.id,
                func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
                func.trim(FamilyMember.contact_phone).in_(candidates),
            )
            .exists()
        )

        conditions = [
            func.trim(Member.profile_data["spouse_mobile"].astext).in_(candidates),
            spouse_phone_match,
        ]

        stmt = select(Member).where(
            Member.is_deleted.is_(False),
            or_(*conditions),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def check_phone_in_use(
        self,
        phone: str,
        *,
        exclude_member_id: UUID | None = None,
        exclude_user_id: UUID | None = None,
    ) -> tuple[bool, str | None]:
        """Check if a phone number exists as a member mobile or spouse mobile in DB.

        Returns (True, "member_phone") if matched on user or member primary phone,
        (True, "spouse_phone") if matched on member or family spouse phone,
        or (False, None) if not present.
        """
        candidates = self._phone_candidates(phone)

        # 1. Check users table
        user_stmt = select(User.id).where(
            func.trim(User.phone_number).in_(candidates)
        )
        if exclude_user_id is not None:
            user_stmt = user_stmt.where(User.id != exclude_user_id)
        user_res = (await self.session.execute(user_stmt)).scalars().first()
        if user_res is not None:
            return True, "member_phone"

        # 2. Check members table primary mobile
        member_conditions = [
            func.trim(Member.contact_phone).in_(candidates),
            func.trim(Member.profile_data["phone"].astext).in_(candidates),
            func.trim(Member.profile_data["mobile_number"].astext).in_(candidates),
        ]
        member_stmt = select(Member.id).where(
            Member.is_deleted.is_(False),
            or_(*member_conditions),
        )
        if exclude_member_id is not None:
            member_stmt = member_stmt.where(Member.id != exclude_member_id)
        if exclude_user_id is not None:
            member_stmt = member_stmt.where(
                or_(Member.user_id != exclude_user_id, Member.user_id.is_(None))
            )
        member_res = (await self.session.execute(member_stmt)).scalars().first()
        if member_res is not None:
            return True, "member_phone"

        # 3. Check spouse mobile in members.profile_data or family_members
        spouse_rel_types = ["spouse", "wife", "husband"]
        spouse_fam_match = (
            select(FamilyMember.id)
            .where(
                FamilyMember.member_id == Member.id,
                func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
                func.trim(FamilyMember.contact_phone).in_(candidates),
            )
            .exists()
        )
        spouse_conditions = [
            func.trim(Member.profile_data["spouse_mobile"].astext).in_(candidates),
            spouse_fam_match,
        ]
        spouse_stmt = select(Member.id).where(
            Member.is_deleted.is_(False),
            or_(*spouse_conditions),
        )
        if exclude_member_id is not None:
            spouse_stmt = spouse_stmt.where(Member.id != exclude_member_id)
        if exclude_user_id is not None:
            spouse_stmt = spouse_stmt.where(
                or_(Member.user_id != exclude_user_id, Member.user_id.is_(None))
            )
        spouse_res = (await self.session.execute(spouse_stmt)).scalars().first()
        if spouse_res is not None:
            return True, "spouse_phone"

        return False, None

    async def list_by_group(
        self, group_id, limit: int = 100, offset: int = 0
    ) -> list[Member]:
        result = await self.session.execute(
            select(Member)
            .where(member_group_filter(group_id), Member.is_deleted.is_(False))
            .order_by(Member.first_name)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def search(
        self,
        query: str,
        group_id=None,
        membership_status: str | None = None,
        group_name: str | None = None,
        designation: str | None = None,
        trustee_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Member]:
        where_cond, rank_expr = _build_member_search_expressions(query)
        stmt = (
            select(Member)
            .outerjoin(Location, Location.id == Member.location_id)
            .outerjoin(SocialGroup, SocialGroup.id == Member.group_id)
            .options(*MEMBER_PROFILE_LOADS)
            .where(Member.is_deleted.is_(False))
        )
        if where_cond is not None:
            stmt = stmt.where(where_cond)
        if group_id is not None:
            stmt = stmt.where(member_group_filter(group_id))
        if membership_status is not None:
            stmt = stmt.where(Member.membership_status == membership_status)
        if group_name:
            stmt = stmt.where(group_name_filter(group_name))
        if trustee_only:
            stmt = stmt.where(trustee_role_filter())
        if (designation or "").strip():
            stmt = stmt.where(designation_filter(designation.strip()))

        if rank_expr is not None:
            stmt = stmt.order_by(
                rank_expr.asc(), Member.first_name.asc(), Member.last_name.asc()
            )
        else:
            stmt = stmt.order_by(Member.first_name.asc(), Member.last_name.asc())

        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def search_detailed(
        self,
        query: str,
        group_id=None,
        membership_status: str | None = None,
        group_name: str | None = None,
        location: str | None = None,
        member_id=None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Member], int]:
        """Keyword search across every member field, plus the total count.

        Matches the member's own columns, full name combinations, spouse name,
        linked group, linked location (city, state), phone, membership number,
        and profile fields. Natural ranking prioritizes exact/prefix name matches.
        """
        where_cond, rank_expr = _build_member_search_expressions(query)
        location_needle = f"%{location}%" if location else None

        stmt = (
            select(Member)
            .outerjoin(Location, Location.id == Member.location_id)
            .outerjoin(SocialGroup, SocialGroup.id == Member.group_id)
            .options(*MEMBER_PROFILE_LOADS)
            .where(Member.is_deleted.is_(False))
        )
        if where_cond is not None:
            stmt = stmt.where(where_cond)
        if group_id is not None:
            stmt = stmt.where(member_group_filter(group_id))
        if member_id is not None:
            stmt = stmt.where(Member.id == member_id)
        if membership_status is not None:
            stmt = stmt.where(Member.membership_status == membership_status)
        if group_name:
            stmt = stmt.where(group_name_filter(group_name))
        if location_needle is not None:
            stmt = stmt.where(
                or_(
                    Location.name.ilike(location_needle),
                    Location.city.ilike(location_needle),
                    Location.area.ilike(location_needle),
                    Location.state.ilike(location_needle),
                    Member.profile_data["city"].astext.ilike(location_needle),
                    Member.profile_data["state"].astext.ilike(location_needle),
                    Member.profile_data["area"].astext.ilike(location_needle),
                )
            )

        total = (
            await self.session.execute(
                select(func.count()).select_from(stmt.subquery())
            )
        ).scalar_one()

        if rank_expr is not None:
            stmt = stmt.order_by(
                rank_expr.asc(), Member.first_name.asc(), Member.last_name.asc()
            )
        else:
            stmt = stmt.order_by(Member.first_name.asc(), Member.last_name.asc())

        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all()), int(total)
