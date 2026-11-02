from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.core.permissions import Permission
from app.core.responses import ok
from app.core.scope import AccessScope
from app.dependencies import CurrentUser, DBSession, require_permission
from app.services.celebration_service import CelebrationService
from app.utils.helpers import parse_any_date

router = APIRouter(prefix="/celebrations", tags=["Celebrations"])

READ = Depends(require_permission(Permission.MEMBER_READ))


@router.get("/birthdays", dependencies=[READ])
async def list_todays_birthdays(
    db: DBSession,
    current_user: CurrentUser,
    on: str | None = Query(
        default=None,
        alias="date",
        description="Day to celebrate (DD/MM/YYYY or YYYY-MM-DD); defaults to today",
    ),
):
    """Today's birthdays, for the member and their spouse.

    Each item carries both people (name + photo) and marks whether it is the
    member's or the spouse's birthday, plus their group and location.

    All authenticated users (Group Admin, Member, Super Admin) see all
    community birthdays - no group restriction applied.
    """
    target = parse_any_date(on) if on else date.today()
    if target is None:
        target = date.today()

    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # scope = await get_access_scope(db, current_user)
    # items = await CelebrationService(db).birthdays(scope, target)

    # Use unrestricted scope so all roles see all community celebrations
    _ = current_user  # keep dependency for auth; scope not used for filtering
    unrestricted_scope = AccessScope(unrestricted=True, group_id=None, member_id=None)
    items = await CelebrationService(db).birthdays(unrestricted_scope, target)
    return ok(
        "Today's birthdays fetched successfully",
        [item.model_dump(mode="json") for item in items],
    )


@router.get("/anniversaries", dependencies=[READ])
async def list_todays_anniversaries(
    db: DBSession,
    current_user: CurrentUser,
    on: str | None = Query(
        default=None,
        alias="date",
        description="Day to celebrate (DD/MM/YYYY or YYYY-MM-DD); defaults to today",
    ),
):
    """Today's wedding anniversaries.

    Each item carries the couple (name + photo), how many years they have been
    married, and their group and location.

    All authenticated users (Group Admin, Member, Super Admin) see all
    community anniversaries - no group restriction applied.
    """
    target = parse_any_date(on) if on else date.today()
    if target is None:
        target = date.today()

    # OLD SCOPE RESTRICTION (commented out - restored if needed):
    # scope = await get_access_scope(db, current_user)
    # items = await CelebrationService(db).anniversaries(scope, target)

    # Use unrestricted scope so all roles see all community celebrations
    _ = current_user  # keep dependency for auth; scope not used for filtering
    unrestricted_scope = AccessScope(unrestricted=True, group_id=None, member_id=None)
    items = await CelebrationService(db).anniversaries(unrestricted_scope, target)
    return ok(
        "Today's anniversaries fetched successfully",
        [item.model_dump(mode="json") for item in items],
    )
