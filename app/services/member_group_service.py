"""Membership management for members that belong to more than one social group.

The first membership is the primary one. It owns ``Member.group_id`` and the
visible member ID; later memberships are secondary and never change that
number. Removing the primary promotes the next membership and mints a fresh ID
from the new primary group's counter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenException
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.member_group import GroupMember, MemberGroupMembership
from app.services.group_id_service import assign_member_id


async def _lock_member(db: AsyncSession, member: Member) -> None:
    await db.execute(select(Member.id).where(Member.id == member.id).with_for_update())


async def list_memberships(
    db: AsyncSession, member_id: UUID
) -> list[MemberGroupMembership]:
    result = await db.execute(
        select(MemberGroupMembership)
        .where(MemberGroupMembership.member_id == member_id)
        .order_by(MemberGroupMembership.priority, MemberGroupMembership.created_at)
    )
    return list(result.scalars().all())


async def ensure_legacy_membership(
    db: AsyncSession, member: Member
) -> MemberGroupMembership | None:
    """Materialize ``members.group_id`` as a primary membership when missing."""
    existing = await db.execute(
        select(MemberGroupMembership)
        .where(MemberGroupMembership.member_id == member.id)
        .order_by(MemberGroupMembership.priority)
        .limit(1)
    )
    first = existing.scalar_one_or_none()
    if first is not None or member.group_id is None:
        return first

    row = MemberGroupMembership(
        member=member,
        group_id=member.group_id,
        priority=1,
        joined_at=member.joined_at or datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def add_membership(
    db: AsyncSession, member: Member, group: SocialGroup
) -> tuple[MemberGroupMembership, bool]:
    """Add a group, returning the row and whether it was newly created.

    The first membership becomes primary and is the only one that mints a
    member ID. Adding a secondary group leaves the primary and its ID intact.
    """
    await _lock_member(db, member)
    await ensure_legacy_membership(db, member)

    existing = await db.execute(
        select(MemberGroupMembership).where(
            MemberGroupMembership.member_id == member.id,
            MemberGroupMembership.group_id == group.id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if not member.group_id:
            member.group_id = row.group_id
        if not row.membership_number and group.group_id:
            await assign_member_id(db, member, group)
        return row, False

    highest = await db.execute(
        select(func.max(MemberGroupMembership.priority)).where(
            MemberGroupMembership.member_id == member.id
        )
    )
    max_priority = highest.scalar_one_or_none()
    priority = (max_priority or 0) + 1
    row = MemberGroupMembership(
        member=member,
        group=group,
        priority=priority,
        joined_at=datetime.now(UTC),
    )
    db.add(row)

    if priority == 1:
        member.group_id = group.id
        await db.flush()
        if group.group_id:
            await assign_member_id(db, member, group)
        else:
            member.membership_number = None
    else:
        await db.flush()
        if group.group_id:
            await assign_member_id(db, member, group)
    return row, True


async def remove_membership(
    db: AsyncSession, member: Member, group_id: UUID
) -> tuple[bool, MemberGroupMembership | None]:
    """Remove one membership and promote the next group if it was primary.

    Returns ``(removed, new_primary)``. Removing the last group clears both the
    primary pointer and the visible member ID; the next group for that member
    will mint a new ID when they are added again.
    """
    await _lock_member(db, member)
    await ensure_legacy_membership(db, member)

    result = await db.execute(
        select(MemberGroupMembership)
        .where(
            MemberGroupMembership.member_id == member.id,
            MemberGroupMembership.group_id == group_id,
        )
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False, None

    was_primary = row.priority == 1
    await db.delete(row)
    if member.user_id:
        await db.execute(
            delete(GroupMember).where(
                GroupMember.user_id == member.user_id,
                GroupMember.group_id == group_id,
            )
        )
    await db.flush()

    if not was_primary:
        return True, None

    next_result = await db.execute(
        select(MemberGroupMembership)
        .where(MemberGroupMembership.member_id == member.id)
        .order_by(MemberGroupMembership.priority)
        .limit(1)
    )
    new_primary = next_result.scalar_one_or_none()
    if new_primary is None:
        member.group_id = None
        member.membership_number = None
        await db.flush()
        return True, None

    new_primary.priority = 1
    member.group_id = new_primary.group_id
    group = await db.get(SocialGroup, new_primary.group_id)
    await db.flush()
    if group is not None and group.group_id:
        await assign_member_id(db, member, group)
    else:
        member.membership_number = None
        await db.flush()
    return True, new_primary


async def sync_memberships(
    db: AsyncSession,
    member: Member,
    group_ids: list[UUID],
    *,
    managed_group_id: UUID | None = None,
) -> None:
    """Apply an ordered group selection.

    Existing primary order is preserved; new groups are appended in the order
    the caller supplied. A group admin (``managed_group_id`` set) can only add
    their own group and can never remove a membership in another group.
    """
    desired: list[UUID] = []
    for group_id in group_ids:
        if group_id not in desired:
            desired.append(group_id)
    if not desired:
        raise ForbiddenException("A member must belong to at least one group")

    await _lock_member(db, member)
    await ensure_legacy_membership(db, member)
    current = await list_memberships(db, member.id)
    current_ids = {row.group_id for row in current}
    desired_ids = set(desired)

    for row in current:
        if row.group_id in desired_ids:
            continue
        if managed_group_id is not None and row.group_id != managed_group_id:
            continue
        await remove_membership(db, member, row.group_id)

    for group_id in desired:
        if group_id in current_ids:
            continue
        if managed_group_id is not None and group_id != managed_group_id:
            raise ForbiddenException(
                "You can only add members to your assigned group"
            )
        group = await db.get(SocialGroup, group_id)
        if group is None or group.is_deleted:
            continue
        await add_membership(db, member, group)


async def detach_group(db: AsyncSession, group_id: UUID) -> None:
    """Promote affected members before a group is hard-deleted."""
    result = await db.execute(
        select(MemberGroupMembership.member_id)
        .where(MemberGroupMembership.group_id == group_id)
        .distinct()
    )
    for member_id in list(result.scalars().all()):
        member = await db.get(Member, member_id)
        if member is not None:
            await remove_membership(db, member, group_id)


async def detach_member(db: AsyncSession, member: Member) -> None:
    """Remove all group memberships when a member is deleted."""
    await db.execute(
        delete(MemberGroupMembership).where(MemberGroupMembership.member_id == member.id)
    )
    if member.user_id:
        await db.execute(
            delete(GroupMember).where(GroupMember.user_id == member.user_id)
        )
    member.group_id = None

