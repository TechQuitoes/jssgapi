from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BeforeValidator

from app.core.constants import (
    AuditAction,
    EventTimeFrame,
    EventType,
    RoleName,
    resolve_role_name,
)
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    get_access_scope,
    get_admin_scope,
    member_group_ids,
)
from app.dependencies import (
    CurrentUser,
    DBSession,
    get_current_user,
    require_permission,
)
from sqlalchemy import select
from app.models.event import Event
from app.models.event_group import EventGroup
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.member_group import GroupMember
from app.models.user import User
from app.repositories.event_repository import EventRepository
from app.schemas.event import (
    EventCreate,
    EventDetailOut,
    EventImageOut,
    EventOut,
    EventUpdate,
)
from app.services.audit_service import AuditService
from app.services.event_image_service import (
    MAX_IMAGES_PER_EVENT,
    EventImageService,
    ImageUpload,
)
from app.services.event_service import EventService, apply_timing, resolve_host_defaults
from app.utils.event_datetime import combine_local, get_app_tz, time_frame_bounds

router = APIRouter(prefix="/events", tags=["Events"])

AUTHENTICATED = Depends(get_current_user)
READ = Depends(require_permission(Permission.EVENT_READ))
CREATE = Depends(require_permission(Permission.EVENT_CREATE))
UPDATE = Depends(require_permission(Permission.EVENT_UPDATE))
DELETE = Depends(require_permission(Permission.EVENT_DELETE))


def _upper(value: Any) -> Any:
    """Accept a filter value in any case, e.g. ``future`` as well as ``FUTURE``."""
    return value.strip().upper() if isinstance(value, str) else value


#: ``?time_frame=past|upcoming|future`` - the bucket an event sits in.
TimeFrameQuery = Annotated[
    EventTimeFrame | None,
    BeforeValidator(_upper),
    Query(description="PAST, UPCOMING (within 7 days) or FUTURE"),
]

#: ``?event_type=ANNUAL`` - the kind of programme an event is.
EventTypeQuery = Annotated[
    EventType | None,
    BeforeValidator(_upper),
    Query(description="The kind of programme, e.g. ANNUAL or FUNDRAISER"),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _role_of(user: User) -> RoleName | None:
    if user.role is None:
        return None
    return resolve_role_name(user.role.name)


@dataclass(frozen=True)
class ReadScope:
    """Which events a caller may see.

    Only unrestricted callers include legacy events with no group mapping.
    Scoped callers must match an assigned group, so an ungrouped event cannot
    leak across group boundaries.
    """

    group_ids: set[UUID] | None = None
    include_ungrouped: bool = True
    public_only: bool = False


async def _effective_member_group_ids(
    db: DBSession,
    primary_group_id: UUID | None,
    member_id: UUID | None,
    current_user: User | None = None,
) -> set[UUID]:
    effective: set[UUID] = set()
    if primary_group_id is not None:
        effective.add(primary_group_id)
    if current_user is not None and current_user.group_id is not None:
        effective.add(current_user.group_id)
    if member_id is not None:
        effective.update(await member_group_ids(db, member_id))
    if current_user is not None:
        gm_result = await db.execute(
            select(GroupMember.group_id).where(GroupMember.user_id == current_user.id)
        )
        for gid in gm_result.scalars().all():
            effective.add(gid)
        if member_id is None and current_user.phone_number:
            res = await db.execute(
                select(Member).where(
                    Member.contact_phone == current_user.phone_number,
                    Member.is_deleted.is_(False),
                )
            )
            m = res.scalars().first()
            if m is not None:
                if m.group_id is not None:
                    effective.add(m.group_id)
                effective.update(await member_group_ids(db, m.id))
    return effective


async def _read_scope(
    db: DBSession, current_user: User, requested_group_id: UUID | None
) -> ReadScope:
    """Resolve the groups whose events this caller may list.

    Super Admins always see every live event, including legacy events with no
    group mapping; ``requested_group_id`` never narrows their listing. Group
    Admins are limited to their own group plus all-group (null) events. Members
    see events for every group they belong to, plus all-group (null) events,
    and only public events.
    """
    access = await get_access_scope(db, current_user)
    role = _role_of(current_user)

    if access.unrestricted:
        return ReadScope(group_ids=None, include_ungrouped=True)

    if role is RoleName.GROUP_ADMIN:
        if access.group_id is None:
            return ReadScope(group_ids=set(), include_ungrouped=True)
        if requested_group_id is not None and requested_group_id != access.group_id:
            raise ForbiddenException("Operation is limited to your own group")
        include_ungrouped = requested_group_id is None
        return ReadScope(
            group_ids={access.group_id},
            include_ungrouped=include_ungrouped,
        )

    if role is RoleName.MEMBER:
        effective = await _effective_member_group_ids(
            db, access.group_id, access.member_id, current_user
        )
        if requested_group_id is not None:
            if requested_group_id not in effective:
                raise ForbiddenException("Operation is limited to your groups")
            effective = {requested_group_id}
            include_ungrouped = False
        else:
            include_ungrouped = True
        return ReadScope(
            group_ids=effective,
            include_ungrouped=include_ungrouped,
            public_only=True,
        )

    return ReadScope(group_ids=set(), include_ungrouped=True, public_only=True)


async def _resolve_event_group_ids(
    db: DBSession,
    current_user: User,
    requested_group_ids: list[UUID],
    *,
    creating: bool,
) -> list[UUID]:
    """Validate a group selection and apply the caller's role limits."""
    scope = await get_admin_scope(db, current_user)
    if not requested_group_ids:
        if creating and scope is not None:
            requested_group_ids = [scope]
        elif scope is not None:
            raise BadRequestException(
                "An event must be associated with at least one group"
            )
        else:
            # Super Admin can create or update an event with no groups for platform-wide / All Groups
            return []
    if scope is not None and any(group_id != scope for group_id in requested_group_ids):
        raise ForbiddenException("Operation is limited to your own group")

    resolved: list[UUID] = []
    for group_id in requested_group_ids:
        group = await db.get(SocialGroup, group_id)
        if group is None or group.is_deleted:
            raise BadRequestException("Social group not found")
        if group_id not in resolved:
            resolved.append(group_id)
    return resolved


async def _replace_event_groups(
    db: DBSession, event: Event, group_ids: list[UUID]
) -> None:
    await db.refresh(event, ["event_groups"])
    existing = list(event.event_groups or [])
    keep = {link.group_id for link in existing}
    wanted = set(group_ids)
    for link in existing:
        if link.group_id not in wanted:
            await db.delete(link)
    for group_id in group_ids:
        if group_id not in keep:
            db.add(EventGroup(event_id=event.id, group_id=group_id))
    await db.flush()
    await db.refresh(event, ["event_groups"])


async def _assert_can_manage_event(
    db: DBSession, current_user: User, event: Event
) -> None:
    scope = await get_admin_scope(db, current_user)
    if scope is None:
        return
    if scope not in event.group_ids:
        raise ForbiddenException("Operation is limited to your own group")


def _event_out(event: Event, image_count: int = 0) -> EventOut:
    out = EventOut.model_validate(event)
    out.image_count = image_count
    return out


async def _get_event_or_404(db: DBSession, event_id: UUID) -> Event:
    event = await EventRepository(db).get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    return event


async def _get_visible_event(
    db: DBSession, current_user: User, event_id: UUID
) -> Event:
    """Fetch an event the caller is allowed to see."""
    event = await _get_event_or_404(db, event_id)
    access = await get_access_scope(db, current_user)
    role = _role_of(current_user)

    if access.unrestricted:
        return event

    event_group_ids = set(event.group_ids)

    # An event with no group mapping (All Groups / null group_id) is visible to everyone
    if event.group_id is None or not event_group_ids:
        if role is RoleName.MEMBER and not event.is_public:
            raise NotFoundException("Event", str(event_id))
        return event

    if role is RoleName.GROUP_ADMIN:
        if access.group_id in event_group_ids:
            return event
        raise NotFoundException("Event", str(event_id))

    if role is RoleName.MEMBER:
        if not event.is_public:
            raise NotFoundException("Event", str(event_id))
        effective = await _effective_member_group_ids(
            db, access.group_id, access.member_id, current_user
        )
        if event_group_ids & effective:
            return event
        raise NotFoundException("Event", str(event_id))

    raise NotFoundException("Event", str(event_id))

# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------


@router.get("", dependencies=[AUTHENTICATED])
async def list_events(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    time_frame: TimeFrameQuery = None,
    event_type: EventTypeQuery = None,
    search: str | None = Query(default=None, max_length=255),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List the events a caller may see.

    ``time_frame`` buckets the events by how far away they are: ``past`` has
    already started, ``upcoming`` starts within the next seven days and
    ``future`` starts later than that.
    """
    repo = EventRepository(db)
    scope = await _read_scope(db, current_user, group_id)
    now, window_end = time_frame_bounds()

    total = await repo.count_filtered(
        group_ids=scope.group_ids,
        include_ungrouped=scope.include_ungrouped,
        public_only=scope.public_only,
        time_frame=time_frame,
        event_type=event_type,
        search=search,
        now=now,
        window_end=window_end,
    )
    items = await repo.list_filtered(
        group_ids=scope.group_ids,
        include_ungrouped=scope.include_ungrouped,
        public_only=scope.public_only,
        time_frame=time_frame,
        event_type=event_type,
        search=search,
        now=now,
        window_end=window_end,
        order=order,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    images = EventImageService(db)
    counts = await images.batch_count_images(items)
    data = [_event_out(e, counts.get(e.id, 0)) for e in items]
    return paginated(
        "Events fetched successfully",
        data,
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/upcoming", dependencies=[READ])
async def list_upcoming_events(
    db: DBSession,
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=100),
):
    """Events starting within the next seven days, soonest first."""
    return await _list_by_frame(db, current_user, EventTimeFrame.UPCOMING, limit)


@router.get("/past", dependencies=[READ])
async def list_past_events(
    db: DBSession,
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=100),
):
    """Events that have already started, most recent first."""
    return await _list_by_frame(db, current_user, EventTimeFrame.PAST, limit)


@router.get("/future", dependencies=[READ])
async def list_future_events(
    db: DBSession,
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=100),
):
    """Events starting more than seven days from now, soonest first."""
    return await _list_by_frame(db, current_user, EventTimeFrame.FUTURE, limit)


async def _list_by_frame(
    db: DBSession,
    current_user: CurrentUser,
    frame: EventTimeFrame,
    limit: int,
):
    repo = EventRepository(db)
    scope = await _read_scope(db, current_user, None)
    now, window_end = time_frame_bounds()
    items = await repo.list_filtered(
        group_ids=scope.group_ids,
        include_ungrouped=scope.include_ungrouped,
        public_only=scope.public_only,
        time_frame=frame,
        now=now,
        window_end=window_end,
        order="asc" if frame is not EventTimeFrame.PAST else "desc",
        limit=limit,
    )
    images = EventImageService(db)
    counts = await images.batch_count_images(items)
    data = [_event_out(e, counts.get(e.id, 0)) for e in items]
    return ok(f"{frame.value.title()} events fetched successfully", data)


@router.get("/event-types", dependencies=[READ])
async def list_event_types():
    """The event types a host can choose from."""
    return ok(
        "Event types fetched successfully",
        [
            {"value": t.value, "label": t.value.replace("_", " ").title()}
            for t in EventType
        ],
    )


# --------------------------------------------------------------------------
# single event
# --------------------------------------------------------------------------


@router.get("/{event_id}", dependencies=[READ])
async def get_event(event_id: UUID, db: DBSession, current_user: CurrentUser):
    event = await _get_visible_event(db, current_user, event_id)
    images = await EventImageService(db).list_images(event)
    detail = EventDetailOut.model_validate(event)
    detail.images = [EventImageOut.model_validate(i) for i in images]
    detail.image_count = len(images)
    return ok("Event fetched successfully", detail)


@router.post("", dependencies=[CREATE])
async def create_event(body: EventCreate, current_user: CurrentUser, db: DBSession):
    """Create an event.

    The host defaults to the account creating it: the uploader's name and
    contact number are filled in unless the request names a different host.
    """
    group_ids = await _resolve_event_group_ids(
        db, current_user, body.requested_group_ids(), creating=True
    )
    data = body.model_dump()
    data.pop("group_ids", None)
    data["group_id"] = group_ids[0] if group_ids else None
    data["event_type"] = body.event_type.value
    data["status"] = body.status.value

    host_defaults = resolve_host_defaults(current_user)
    data["host_id"] = host_defaults["host_id"]
    if not data.get("host_name"):
        data["host_name"] = host_defaults["host_name"]
    if not data.get("host_contact_number"):
        data["host_contact_number"] = host_defaults["host_contact_number"]

    # The instant is derived from the wall-clock date and time, never the
    # other way round, so the two can not drift apart.
    data.pop("starts_at", None)

    event = Event(**data)
    apply_timing(event)
    db.add(event)
    await db.flush()
    await _replace_event_groups(db, event, group_ids)
    await db.refresh(event)

    await AuditService(db).log(
        AuditAction.EVENT_CREATE,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
    )
    await db.flush()
    return created("Event created successfully", _event_out(event))


@router.patch("/{event_id}", dependencies=[UPDATE])
async def update_event(
    event_id: UUID, body: EventUpdate, current_user: CurrentUser, db: DBSession
):
    """Update an event. Only the fields that are sent change."""
    repo = EventRepository(db)
    event = await repo.get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    await _assert_can_manage_event(db, current_user, event)
    requested_group_ids = body.requested_group_ids()
    group_ids: list[UUID] | None = None
    if requested_group_ids is not None:
        group_ids = await _resolve_event_group_ids(
            db, current_user, requested_group_ids, creating=False
        )

    data = body.model_dump(exclude_unset=True, exclude_none=True)
    data.pop("starts_at", None)
    data.pop("group_ids", None)
    data.pop("group_id", None)
    if group_ids is not None:
        data["group_id"] = group_ids[0] if group_ids else None
        event.group_id = data["group_id"]
    if body.event_type is not None:
        data["event_type"] = body.event_type.value
    for key, value in data.items():
        setattr(event, key, value)
    if body.event_type is not None and body.event_type is not EventType.OTHER:
        # The free-text label only means anything alongside OTHER.
        event.event_type_other = None

    # The instant always follows the wall-clock date and time.
    if body.event_date is not None or body.event_time is not None:
        apply_timing(event)
    elif body.starts_at is not None:
        # A client that only speaks the old timestamp gets its date and time
        # read back out of it.
        local = body.starts_at.astimezone(get_app_tz())
        apply_timing(event, event_date=local.date(), event_time=local.time())
    else:
        event.starts_at = combine_local(event.event_date, event.event_time)

    event = await repo.update(event)
    if group_ids is not None:
        await _replace_event_groups(db, event, group_ids)
    await AuditService(db).log(
        AuditAction.EVENT_UPDATE,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
    )
    await db.flush()
    return ok("Event updated successfully", _event_out(event))


@router.delete("/{event_id}", dependencies=[DELETE])
async def delete_event(event_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = EventRepository(db)
    event = await repo.get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    await _assert_can_manage_event(db, current_user, event)

    # The stored objects go with the event, not just the rows.
    await EventImageService(db).delete_all_for_event(event)
    await EventService(db).remove_cover(event)
    await repo.delete(event)
    await AuditService(db).log(
        AuditAction.EVENT_DELETE,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
    )
    await db.flush()
    return ok("Event deleted successfully")


# --------------------------------------------------------------------------
# cover image (the event's profile picture)
# --------------------------------------------------------------------------


@router.post("/{event_id}/cover", dependencies=[UPDATE])
async def upload_event_cover(
    event_id: UUID,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    """Upload or replace the event's cover image."""
    event = await _get_event_or_404(db, event_id)
    await _assert_can_manage_event(db, current_user, event)

    content = await file.read()
    url = await EventService(db).upload_cover(
        event,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.EVENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
        details={"field": "cover_image", "url": url},
    )
    await db.flush()
    # ``updated_at`` is set by the database, so the row is read back before it
    # is turned into a response.
    await db.refresh(event)
    return created("Event cover image uploaded successfully", _event_out(event))


@router.delete("/{event_id}/cover", dependencies=[UPDATE])
async def delete_event_cover(
    event_id: UUID, current_user: CurrentUser, db: DBSession
):
    """Remove the event's cover image."""
    event = await _get_event_or_404(db, event_id)
    await _assert_can_manage_event(db, current_user, event)
    await EventService(db).remove_cover(event)
    await db.flush()
    await db.refresh(event)
    return ok("Event cover image deleted successfully", _event_out(event))


# --------------------------------------------------------------------------
# gallery images
# --------------------------------------------------------------------------


@router.get("/{event_id}/images", dependencies=[AUTHENTICATED])
async def list_event_images(
    event_id: UUID, db: DBSession, current_user: CurrentUser
):
    """The event's gallery, in upload order."""
    event = await _get_visible_event(db, current_user, event_id)
    images = await EventImageService(db).list_images(event)
    return ok(
        "Event images fetched successfully",
        [EventImageOut.model_validate(i) for i in images],
    )


@router.post("/{event_id}/images", dependencies=[UPDATE])
async def upload_event_images(
    event_id: UUID,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    files: list[UploadFile] = File(...),
):
    """Upload images to the event's gallery.

    An event holds at most ten images. Both the size of this request and the
    number the event already holds are checked, so neither a single oversized
    request nor a series of requests can push an event past the limit.
    """
    event = await _get_event_or_404(db, event_id)
    await _assert_can_manage_event(db, current_user, event)

    if not files:
        raise BadRequestException("At least one image is required", "NO_IMAGES")
    if len(files) > MAX_IMAGES_PER_EVENT:
        raise BadRequestException(
            f"An event can hold at most {MAX_IMAGES_PER_EVENT} images",
            "TOO_MANY_IMAGES",
        )

    uploads = [
        ImageUpload(
            file_name=f.filename,
            content=await f.read(),
            mime_type=f.content_type,
        )
        for f in files
    ]

    images = await EventImageService(db).upload_images(
        event,
        uploads,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.EVENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
        details={"count": len(images)},
    )
    await db.flush()
    return created(
        "Event images uploaded successfully",
        [EventImageOut.model_validate(i) for i in images],
    )


@router.delete("/{event_id}/images/{image_id}", dependencies=[UPDATE])
async def delete_event_image(
    event_id: UUID,
    image_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
):
    """Remove one image from the event's gallery, freeing a slot."""
    event = await _get_event_or_404(db, event_id)
    await _assert_can_manage_event(db, current_user, event)

    service = EventImageService(db)
    image = await service.find_image(event, image_id)
    await service.delete_image(event, image)
    await db.flush()
    return ok("Event image deleted successfully")
