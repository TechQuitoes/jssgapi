from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, or_, select

from app.core.constants import MemberStatus
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession
from app.models.group import SocialGroup
from app.models.location import Location
from app.models.member import Member
from app.repositories.member_repository import trustee_role_filter
from app.schemas.dashboard import DashboardStatisticsOut

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/statistics")
@router.get("/stats", include_in_schema=False)
async def get_dashboard_statistics(
    db: DBSession,
    current_user: CurrentUser,
):
    """Return dashboard summary counts.

    Accessible to all authenticated users (Super Admin, Group Admin, Member).
    Computes all 4 counts concurrently in a single database round-trip:
    - total_members: Approved, non-deleted members.
    - total_groups: Active, non-deleted social groups.
    - total_post_holders: Approved, non-deleted members qualifying as post holders (trustees).
    - total_cities: Unique cities from active social groups (case-insensitive & whitespace trimmed).
    """
    _ = current_user  # Authentication dependency enforced

    # 1. Total members count (Approved, non-deleted)
    members_subquery = (
        select(func.count(Member.id))
        .where(
            Member.is_deleted.is_(False),
            Member.membership_status == MemberStatus.APPROVED.value,
        )
        .scalar_subquery()
    )

    # 2. Total groups count (Active, non-deleted)
    groups_subquery = (
        select(func.count(SocialGroup.id))
        .where(
            SocialGroup.is_deleted.is_(False),
            SocialGroup.is_active.is_(True),
            SocialGroup.status == "ACTIVE",
        )
        .scalar_subquery()
    )

    # 3. Total post holders count (Approved, non-deleted, matching post-holder filter)
    post_holders_subquery = (
        select(func.count(Member.id))
        .where(
            Member.is_deleted.is_(False),
            Member.membership_status == MemberStatus.APPROVED.value,
            trustee_role_filter(),
        )
        .scalar_subquery()
    )

    # 4. Total cities count (Distinct city names from active groups)
    cities_subquery = (
        select(
            func.count(
                func.distinct(
                    func.lower(
                        func.coalesce(
                            func.nullif(func.trim(SocialGroup.city), ""),
                            func.nullif(func.trim(Location.city), ""),
                        )
                    )
                )
            )
        )
        .outerjoin(Location, Location.id == SocialGroup.location_id)
        .where(
            SocialGroup.is_deleted.is_(False),
            SocialGroup.is_active.is_(True),
            SocialGroup.status == "ACTIVE",
            or_(
                func.coalesce(func.trim(SocialGroup.city), "") != "",
                func.coalesce(func.trim(Location.city), "") != "",
            ),
        )
        .scalar_subquery()
    )

    stmt = select(
        members_subquery.label("total_members"),
        groups_subquery.label("total_groups"),
        post_holders_subquery.label("total_post_holders"),
        cities_subquery.label("total_cities"),
    )

    result = await db.execute(stmt)
    row = result.one()

    data = DashboardStatisticsOut(
        total_members=row.total_members or 0,
        total_groups=row.total_groups or 0,
        total_post_holders=row.total_post_holders or 0,
        total_cities=row.total_cities or 0,
    )

    return ok("Dashboard statistics fetched successfully", data.model_dump())
