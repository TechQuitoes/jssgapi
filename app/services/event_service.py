"""Event host details, timing and the cover image.

Two ideas live here:

* the **host** - who is running the event. It defaults to the account that
  created the event, so a host never has to type their own name and number
  again, but it stays editable for the times a different person is on the
  ground.
* the **cover image** - the event's profile picture. It is a single image
  stored on the event itself, separate from the gallery of up to ten images
  that ``EventImageService`` manages.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EventTimeFrame
from app.models.event import Event
from app.models.user import User
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.event_datetime import classify_time_frame, combine_local
from app.utils.file_upload import MIME_SUFFIX, validate_image_upload

logger = logging.getLogger(__name__)

#: Object key template for a cover image.
COVER_OBJECT_KEY_TEMPLATE = "events/{event_id}/cover/{unique}{suffix}"


def resolve_host_defaults(user: User) -> dict[str, object]:
    """The host details of the account that is creating the event.

    The uploader is the host, so their name and contact number are the
    defaults. A host who wants a different contact number for one event
    overrides it on the request.
    """
    return {
        "host_id": user.id,
        "host_name": user.full_name,
        "host_contact_number": user.phone_number,
    }


def apply_timing(
    event: Event,
    *,
    event_date: date | None = None,
    event_time: time | None = None,
) -> Event:
    """Set the event's date and time and re-derive its instant.

    ``starts_at`` is always the instant the stored wall-clock date and time
    refer to, so the two can never drift apart.
    """
    if event_date is not None:
        event.event_date = event_date
    if event_time is not None:
        event.event_time = event_time
    event.starts_at = combine_local(event.event_date, event.event_time)
    return event


def time_frame_of(event: Event) -> EventTimeFrame:
    """Where the event sits relative to now."""
    return classify_time_frame(event.event_date, event.event_time)


class EventService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.storage = storage or get_storage()
        self.media_repo = MediaRepository(session)

    async def upload_cover(
        self,
        event: Event,
        *,
        file_name: str | None,
        content: bytes,
        mime_type: str | None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        """Store the event's profile picture and return the URL to serve it from.

        The new object is uploaded *before* the row is pointed at it, so a
        storage failure leaves the previous cover in place rather than an
        event pointing at an object that does not exist. The object it
        replaces is deleted afterwards, best effort.
        """
        safe_name, safe_mime = validate_image_upload(
            file_name, mime_type, content, error_code="INVALID_COVER_IMAGE"
        )
        object_key = COVER_OBJECT_KEY_TEMPLATE.format(
            event_id=event.id,
            unique=uuid.uuid4().hex,
            suffix=MIME_SUFFIX[safe_mime],
        )
        previous_key = (event.cover_image_object_key or "").strip() or None

        media = await MediaService(self.session, storage=self.storage).upload(
            owner_type="event",
            owner_id=event.id,
            file_name=safe_name,
            content=content,
            mime_type=safe_mime,
            is_public=True,
            group_id=event.group_id,
            uploaded_by=uploaded_by,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
            object_key=object_key,
        )

        event.cover_image_url = media.url
        event.cover_image_thumb_url = media.thumb_url or media.url
        event.cover_image_object_key = object_key
        await self.session.flush()

        if previous_key and previous_key != object_key:
            await self._discard_object(previous_key)
        return media.url

    async def remove_cover(self, event: Event) -> None:
        """Drop the cover image: clear the reference, then delete the object."""
        object_key = (event.cover_image_object_key or "").strip() or None
        event.cover_image_url = None
        event.cover_image_thumb_url = None
        event.cover_image_object_key = None
        await self.session.flush()
        if object_key:
            await self._discard_object(object_key)

    async def _discard_object(self, object_key: str) -> None:
        """Delete a stored object and its tracking row.

        Storage being unreachable must not undo the change that was already
        committed to Postgres, so failures are logged rather than raised. The
        row is dropped either way: it would otherwise claim an object that no
        longer exists.
        """
        try:
            self.storage.delete_object(object_key)
        except Exception:
            logger.warning(
                "Could not delete media object %s", object_key, exc_info=True
            )
        media = await self.media_repo.get_by_object_key(object_key)
        if media is not None:
            await self.media_repo.delete(media)
