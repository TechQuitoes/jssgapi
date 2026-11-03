from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, and_, func, not_, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.location import Location
    from app.models.member import Member
    from app.models.member_group import MemberGroupMembership
    from app.models.trustee import Trustee
    from app.models.user import User


def _admin_join_condition():
    from app.models.member import Member
    from app.models.user import Role, User

    return and_(
        SocialGroup.id == foreign(User.group_id),
        User.is_deleted.is_(False),
        User.role_id
        == select(Role.id)
        .where(func.upper(func.trim(Role.name)) == "GROUP_ADMIN")
        .scalar_subquery(),
        not_(
            select(Member.id)
            .where(
                Member.user_id == User.id,
                Member.is_deleted.is_(True),
            )
            .exists()
        ),
    )


class SocialGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "social_groups"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: Server-generated business code, e.g. ``MP09001`` - state code + city code
    #: + per-city sequence. Never accepted from a client and never rewritten by
    #: an update; NULL only for rows materialized before ID generation existed
    #: (sheet/directory imports), which are left untouched.
    group_id: Mapped[str | None] = mapped_column(
        String(16), nullable=True, unique=True
    )
    #: Canonical, registry-validated place names the ``group_id`` was built from.
    #: Nullable so pre-existing rows keep working; ``POST /groups`` requires both.
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    location: Mapped[Location | None] = relationship(back_populates="groups")
    members: Mapped[list[Member]] = relationship(back_populates="group")
    member_memberships: Mapped[list[MemberGroupMembership]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    trustees: Mapped[list[Trustee]] = relationship(back_populates="group")
    # Login accounts assigned to administer this group. Only GROUP_ADMIN users
    # belong here; `User.group_id` points back at this row.
    admins: Mapped[list[User]] = relationship(
        "User",
        primaryjoin=_admin_join_condition,
        viewonly=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<SocialGroup {self.name}>"


class GroupIdCounter(Base):
    """PostgreSQL-backed per-city sequence for :attr:`SocialGroup.group_id`.

    One row per city prefix (``MP09`` for Indore, ``MH15`` for Nashik).
    ``last_value`` is advanced with an atomic
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``, so concurrent creates
    in the same city are serialized by the unique index on ``city_key`` and can
    never be handed the same number. It lives in the database rather than in
    process memory so the sequence survives restarts and never reissues a
    number after a group is hard-deleted.
    """

    __tablename__ = "group_id_counters"

    #: The ``state_code + city_code`` prefix the sequence belongs to, e.g. "MP09".
    city_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    #: Highest sequence value handed out for this city. Written only by the
    #: atomic upsert in ``app.services.group_id_service``; never reduced.
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<GroupIdCounter {self.city_key}={self.last_value}>"


class MemberIdCounter(Base):
    """PostgreSQL-backed per-group sequence for ``members.membership_number``.

    One row per Group ID (``MP09001`` for the first Indore group). The member
    ID is the Group ID plus the sequence - ``MP09001001`` is the first member
    added to that group, ``MP09001002`` the second. Allocation uses the same
    atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` upsert as
    :class:`GroupIdCounter`, so concurrent adds in one group are serialized by
    the primary key and can never receive the same member ID. The counter only
    moves forward: a number is never reused after a member leaves or is
    removed from the group.
    """

    __tablename__ = "member_id_counters"

    #: The owning Group ID the sequence belongs to, e.g. "MP09001".
    group_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    #: Highest member sequence handed out for this group. Written only by the
    #: atomic upsert in ``app.services.group_id_service``; never reduced.
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<MemberIdCounter {self.group_id}={self.last_value}>"


class DistrictGroupCounter(Base):
    """Counter table for Groups per District (Prevents collision on delete/recreate)."""

    __tablename__ = "district_group_counters"

    state_code: Mapped[str] = mapped_column(String(2), primary_key=True)
    district_code: Mapped[str] = mapped_column(String(2), primary_key=True)
    last_group_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<DistrictGroupCounter {self.state_code}{self.district_code}={self.last_group_seq}>"


class GroupMemberCounter(Base):
    """Counter table for Members per Group."""

    __tablename__ = "group_member_counters"

    group_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_member_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<GroupMemberCounter {self.group_uuid}={self.last_member_seq}>"
