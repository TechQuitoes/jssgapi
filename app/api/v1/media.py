from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.constants import RoleName, resolve_role_name
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import AccessScope, get_access_scope
from app.dependencies import (
    CurrentUser,
    DBSession,
    OptionalUser,
    require_permission,
)
from app.models.advertisement import Advertisement
from app.models.event import Event
from app.models.jain_tirth import JainTirth
from app.models.media import Media
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.user import User
from app.repositories.media_repository import MediaRepository
from app.schemas.media import MediaOut
from app.services.drive_photo_migration import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DrivePhotoMigration,
)
from app.services.media_service import MediaService

router = APIRouter(prefix="/media", tags=["Media"])

READ = Depends(require_permission(Permission.MEDIA_READ))
UPLOAD = Depends(require_permission(Permission.MEDIA_UPLOAD))
DELETE = Depends(require_permission(Permission.MEDIA_DELETE))
MIGRATE = Depends(require_permission(Permission.MEDIA_MIGRATE))


@router.get("", dependencies=[READ])
async def list_media(
    db: DBSession,
    current_user: CurrentUser,
    owner_type: str | None = Query(default=None),
    owner_id: UUID | None = Query(default=None),
    group_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List media, either by owner or by group.

    Exactly one of ``owner_type``/``owner_id`` or ``group_id`` selects the set.
    A scoped admin is always filtered to its own group, and a request that mixes
    an owner with a group is rejected rather than silently resolved to one of them.
    """
    if (owner_type is None) != (owner_id is None):
        raise BadRequestException("owner_type and owner_id must be supplied together")
    if owner_type is not None and group_id is not None:
        raise BadRequestException("Supply either an owner or a group, not both")
    if owner_type is None and group_id is None:
        raise BadRequestException("Supply either owner_type/owner_id or group_id")

    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")

    repo = MediaRepository(db)
    if owner_type is not None:
        # Owner-based access is about the owner, not the group: a member's own
        # photos stay reachable by that member. A scoped admin may only browse the
        # media of an owner that belongs to its own group.
        if not scope.unrestricted:
            await assert_owner_in_scope(db, scope, owner_type, owner_id)
        items = await repo.list_for_owner(
            owner_type, owner_id, limit=page_size, offset=(page - 1) * page_size
        )
        total = await repo.count_for_owner(owner_type, owner_id)
    else:
        effective_group = None if scope.unrestricted else scope.group_id
        items = await repo.list_for_group(
            effective_group, limit=page_size, offset=(page - 1) * page_size
        )
        total = await repo.count_for_group(effective_group)
    return paginated(
        "Media fetched successfully",
        [MediaOut.model_validate(m) for m in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


async def assert_owner_in_scope(
    db: DBSession, scope: AccessScope, owner_type: str, owner_id: UUID | None
) -> None:
    """Refuse owner-based media browsing outside the caller's group.

    Only the owner types the platform actually creates are recognised; an
    unrecognised ``owner_type`` is refused rather than allowed through, since a
    scoped admin would otherwise be browsing an unenforced dimension.
    """
    if owner_type == "member":
        owner_groups = set(
            (
                await db.execute(
                    select(MemberGroupMembership.group_id).where(
                        MemberGroupMembership.member_id == owner_id
                    )
                )
            )
            .scalars()
            .all()
        )
        owner_group_id = await db.scalar(
            select(Member.group_id).where(Member.id == owner_id)
        )
        if owner_group_id is not None:
            owner_groups.add(owner_group_id)
    elif owner_type == "group":
        owner_group_id = owner_id if owner_id is not None else None
    elif owner_type == "event":
        owner_group_id = await db.scalar(
            select(Event.group_id).where(Event.id == owner_id)
        )
    elif owner_type == "jain_tirth":
        owner_group_id = await db.scalar(
            select(JainTirth.group_id).where(JainTirth.id == owner_id)
        )
    elif owner_type == "advertisement":
        owner_group_id = await db.scalar(
            select(Advertisement.group_id).where(Advertisement.id == owner_id)
        )
    else:
        raise ForbiddenException("This media owner type is not accessible")

    if scope.group_id is None:
        raise ForbiddenException("Operation is limited to your own group")
    if owner_type == "member":
        if scope.group_id not in owner_groups:
            raise ForbiddenException("Operation is limited to your own group")
    elif owner_group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")


@router.post("/upload", dependencies=[UPLOAD])
async def upload_media(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
    owner_type: str = Form(...),
    owner_id: UUID = Form(...),
    is_public: bool = Form(default=True),
    group_id: UUID | None = Form(default=None),
):
    content = await file.read()
    service = MediaService(db)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    # A scoped admin can only file into its own group, and the group is derived from
    # the owner rather than trusted from the form field.
    scope = await get_access_scope(db, current_user)
    effective_group = group_id
    if not scope.unrestricted:
        await assert_owner_in_scope(db, scope, owner_type, owner_id)
        effective_group = scope.group_id
    media = await service.upload(
        owner_type=owner_type,
        owner_id=owner_id,
        file_name=file.filename or "unnamed",
        content=content,
        mime_type=file.content_type or "application/octet-stream",
        is_public=is_public,
        group_id=effective_group,
        uploaded_by=current_user.id,
        ip_address=ip,
        user_agent=ua,
        base_url=str(request.base_url),
    )
    return created("Media uploaded successfully", MediaOut.model_validate(media))


async def scope_group_from_owner(
    db: DBSession, owner_type: str, owner_id: UUID
) -> UUID | None:
    """The group an owner belongs to, for stamping a group-admin upload."""
    if owner_type == "group":
        return owner_id
    if owner_type == "member":
        return await db.scalar(select(Member.group_id).where(Member.id == owner_id))
    if owner_type == "event":
        return await db.scalar(select(Event.group_id).where(Event.id == owner_id))
    if owner_type == "jain_tirth":
        return await db.scalar(
            select(JainTirth.group_id).where(JainTirth.id == owner_id)
        )
    if owner_type == "advertisement":
        return await db.scalar(
            select(Advertisement.group_id).where(Advertisement.id == owner_id)
        )
    return None


@router.post("/migrate-drive-photos", dependencies=[MIGRATE])
async def migrate_drive_photos(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    dry_run: bool = Query(
        True,
        description="Report what would change without writing anything. "
        "Set false to actually copy the photos into R2.",
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    member_id: UUID | None = Query(
        None, description="Migrate a single member instead of a batch."
    ),
):
    """Copy member photos out of Google Drive into R2.

    Member photos imported from the Google Sheet are ``drive.google.com`` share
    links, which the mobile app cannot render: an unshared file resolves to an
    ``accounts.google.com`` sign-in page and a shared one to Drive's HTML
    preview. This fetches the bytes once, stores them in the project's R2
    bucket and repoints ``member_photo_link`` / ``profile_photo_url`` at a URL
    that returns ``image/*`` with no cookies and no headers.

    Defaults to ``dry_run=true``. Nothing in Drive is modified: no file is
    shared, renamed, moved or deleted, and the original link is preserved on the
    member as ``*_photo_drive_url``.

    Safe to re-run - a photo is copied at most once and reports
    ``already_migrated`` afterwards. A photo that cannot be read is recorded in
    ``failures`` and does not stop the rest of the batch.
    """
    migration = DrivePhotoMigration(db, base_url=str(request.base_url))
    ip = request.client.host if request.client else None
    summary = await migration.run(
        dry_run=dry_run,
        limit=limit,
        offset=offset,
        member_id=member_id,
        user_id=current_user.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    return ok("Drive photo migration finished", summary.to_dict())


@router.get("/{media_id}/content", include_in_schema=True)
async def get_media_content(
    media_id: UUID,
    db: DBSession,
    user: OptionalUser,
    variant: str | None = Query(default=None),
):
    """Stream the stored bytes.

    This is what makes ``media.url`` and ``media.thumb_url`` renderable:
    uploads without a public CDN domain point here. Public media is served
    anonymously; private media requires the uploader, the owning member, or an admin.
    """
    media = await MediaRepository(db).get_by_id(media_id)
    if media is None or media.is_deleted:
        raise NotFoundException("Media", str(media_id))

    if not media.is_public and not await _can_access_private(db, media, user):
        raise ForbiddenException()

    service = MediaService(db)
    mime_type = media.mime_type
    filename = media.file_name

    if variant == "thumb":
        thumb_bytes = service.read_thumb(media)
        if thumb_bytes is not None:
            content = thumb_bytes
            mime_type = "image/webp"
            filename = f"thumb_{media.file_name}.webp"
        else:
            content = service.read(media)
    else:
        content = service.read(media)

    return StreamingResponse(
        iter([content]),
        media_type=mime_type,
        headers={
            "Content-Length": str(len(content)),
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if media.is_public
                else "private, no-store"
            ),
            # Never let a stored file be interpreted as active content.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.get("/{media_id}/thumb", include_in_schema=True)
async def get_media_thumb(media_id: UUID, db: DBSession, user: OptionalUser):
    """Convenience endpoint to stream the WebP thumbnail directly."""
    return await get_media_content(media_id, db, user, variant="thumb")


async def _can_access_private(db: DBSession, media: Media, user: User | None) -> bool:
    if user is None:
        return False
    if media.uploaded_by is not None and str(media.uploaded_by) == str(user.id):
        return True
    role = getattr(user, "role", None)
    resolved = resolve_role_name(role.name) if role is not None else None
    if resolved is RoleName.SUPER_ADMIN:
        return True
    if resolved is RoleName.GROUP_ADMIN:
        # A Group Admin only reaches private media inside its own group. The
        # blanket "any admin" rule below would let it read another group's files.
        scope = await get_access_scope(db, user)
        return scope.group_id is not None and media.group_id == scope.group_id
    if media.owner_type == "member":
        member_user_id = await db.scalar(
            select(Member.user_id).where(Member.id == media.owner_id)
        )
        return member_user_id is not None and str(member_user_id) == str(user.id)
    return False


@router.get("/{media_id}")
async def get_media(media_id: UUID, db: DBSession):
    media = await MediaRepository(db).get_by_id(media_id)
    if media is None:
        raise NotFoundException("Media", str(media_id))
    return ok("Media fetched successfully", MediaOut.model_validate(media))


@router.delete("/{media_id}", dependencies=[DELETE])
async def delete_media(
    media_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = MediaRepository(db)
    media = await repo.get_by_id(media_id)
    if media is None:
        raise NotFoundException("Media", str(media_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and media.group_id != scope.group_id:
        # Reported as missing so a cross-group id is not confirmed to exist.
        raise NotFoundException("Media", str(media_id))
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    await MediaService(db).delete(
        media, user_id=current_user.id, ip_address=ip, user_agent=ua
    )
    await db.flush()
    return ok("Media deleted successfully")
