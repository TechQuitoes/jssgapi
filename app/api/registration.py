from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.permissions import Permission
from app.core.redis import (
    invalidate_group_cache,
    invalidate_member_cache,
)
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession, require_permission
from app.schemas.registration import (
    SyncGoogleSheetGroupsRequest,
    SyncGoogleSheetPhotosRequest,
    SyncGoogleSheetRequest,
)
from app.services.drive_photo_migration import DrivePhotoMigration
from app.services.registration_sync_service import RegistrationSyncService
from app.services.sheet_groups_service import import_member_groups

router = APIRouter(prefix="/registration", tags=["Registration"])

SYNC = Depends(require_permission(Permission.MEMBER_CREATE))
MIGRATE = Depends(require_permission(Permission.MEDIA_MIGRATE))
GROUP_SYNC = Depends(require_permission(Permission.GROUP_CREATE))


@router.post("/sync-google-sheet", dependencies=[SYNC])
async def sync_google_sheet(
    body: SyncGoogleSheetRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Import a registration Google Sheet into users/members.

    Read-only by default: ``dry_run`` is ``True`` unless the caller explicitly
    sends ``{"dry_run": false}``, and writing additionally requires
    ``GOOGLE_SHEETS_SYNC_ENABLED=true``.
    """
    result = await RegistrationSyncService(db).run(
        body, current_user_id=current_user.id
    )
    if not body.dry_run:
        await invalidate_member_cache()
    return ok(result.message, result.model_dump())


@router.post("/sync-google-sheet-photos", dependencies=[SYNC, MIGRATE])
async def sync_google_sheet_photos(
    body: SyncGoogleSheetPhotosRequest,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Sync the sheet, then copy/replace member and spouse photos in storage.

    First imports the sheet exactly like ``sync-google-sheet`` (creating new
    members, updating existing ones and never saving a duplicate for a repeat
    mobile number or email). Then copies every member's photo - and spouse photo
    - from its Google Drive link into the project's R2 bucket, replacing the
    stored image when the sheet points at a new file.

    ``dry_run`` defaults to ``True``; writing the sheet rows additionally
    requires ``GOOGLE_SHEETS_SYNC_ENABLED=true``. Photos are processed a batch
    at a time: when the photo summary reports ``has_more=true``, call again with
    ``photo_offset`` advanced by ``photo_limit`` until it is ``false``.
    """
    sync_result = await RegistrationSyncService(db).run(
        SyncGoogleSheetRequest(
            dry_run=body.dry_run,
            spreadsheet_id=body.spreadsheet_id,
            sheet_name=body.sheet_name,
            header_row=body.header_row,
            data_start_row=body.data_start_row,
            limit=body.limit,
        ),
        current_user_id=current_user.id,
    )

    migration = DrivePhotoMigration(db, base_url=str(request.base_url))
    ip = request.client.host if request.client else None
    photo_summary = await migration.run(
        dry_run=body.dry_run,
        limit=body.photo_limit,
        offset=body.photo_offset,
        user_id=current_user.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    photos = photo_summary.to_dict()
    photos["next_offset"] = (
        body.photo_offset + body.photo_limit if photo_summary.has_more else None
    )
    message = (
        sync_result.message
        + " Photos: "
        + (
            f"{photo_summary.migrated} copied, {photo_summary.replaced} replaced, "
            f"{photo_summary.already_migrated} already current, "
            f"{photo_summary.failed} failed"
        )
        + ("" if photo_summary.has_more else ".")
    )
    if not body.dry_run:
        await invalidate_member_cache()
    return ok(
        message,
        {
            "sync": sync_result.model_dump(),
            "photos": photos,
        },
    )


@router.post("/sync-google-sheet-groups", dependencies=[GROUP_SYNC])
async def sync_google_sheet_groups(
    body: SyncGoogleSheetGroupsRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Save every group name (sheet + member directory) into ``social_groups``.

    Idempotent: a name that already exists case/padding-insensitively is kept,
    not duplicated. Members whose free-text group name matches are linked to the
    group row so the Groups API lists them and delete unlinks them cleanly.
    """
    summary = await import_member_groups(
        db,
        sheet_names=None if body.include_sheet else (),
        include_member_data=body.include_member_data,
        link_members=body.link_members,
    )
    await invalidate_group_cache()
    await invalidate_member_cache()
    return ok(summary.message, summary.to_dict())
