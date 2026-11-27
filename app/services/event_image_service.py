"""An event's gallery: up to ten images.

The cover image is not part of this - it is the event's profile picture and
lives on the event itself (see ``EventService``). A gallery image is a link
between the event and the ``media`` row that tracks the stored object.

The limit is hard: an event holds at most ``MAX_IMAGES_PER_EVENT`` images.
It is checked twice - once against the size of the request and once against
what the event already holds - so neither a single oversized request nor a
series of requests can push an event past the limit.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestException, NotFoundException
from app.models.event import Event
from app.models.event_image import EventImage
from app.repositories.event_image_repository import EventImageRepository
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import MIME_SUFFIX, validate_image_upload

logger = logging.getLogger(__name__)

#: The most images one event may hold.
MAX_IMAGES_PER_EVENT = 10

#: Object key template for a gallery image.
IMAGE_OBJECT_KEY_TEMPLATE = "events/{event_id}/gallery/{unique}{suffix}"


@dataclass(frozen=True)
class ImageUpload:
    """One file on its way into a gallery."""

    file_name: str | None
    content: bytes
    mime_type: str | None


class EventImageService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.storage = storage or get_storage()
        self.repo = EventImageRepository(session)
        self.media_repo = MediaRepository(session)

    async def list_images(self, event: Event) -> list[EventImage]:
        return await self.repo.list_for_event(event.id)

    async def find_image(self, event: Event, image_id: uuid.UUID) -> EventImage:
        """Fetch a gallery image that belongs to this event."""
        image = await self.repo.get_by_id(image_id)
        if image is None or image.event_id != event.id:
            raise NotFoundException("Event image", str(image_id))
        return image

    async def count_images(self, event: Event) -> int:
        return await self.repo.count_for_event(event.id)

    async def batch_count_images(
        self, events: list[Event]
    ) -> dict[uuid.UUID, int]:
        return await self.repo.batch_count_for_events([e.id for e in events])

    async def upload_images(
        self,
        event: Event,
        uploads: list[ImageUpload],
        *,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> list[EventImage]:
        """Store a batch of gallery images for an event.

        The whole batch is checked against the limit before anything is
        written, so a request that would exceed it uploads nothing at all.
        """
        existing = await self.count_images(event)
        self._assert_within_limit(existing, len(uploads))

        media_service = MediaService(self.session, storage=self.storage)
        position = existing
        created: list[EventImage] = []
        for upload in uploads:
            safe_name, safe_mime = validate_image_upload(
                upload.file_name,
                upload.mime_type,
                upload.content,
                error_code="INVALID_EVENT_IMAGE",
            )
            object_key = IMAGE_OBJECT_KEY_TEMPLATE.format(
                event_id=event.id,
                unique=uuid.uuid4().hex,
                suffix=MIME_SUFFIX[safe_mime],
            )
            media = await media_service.upload(
                owner_type="event",
                owner_id=event.id,
                file_name=safe_name,
                content=upload.content,
                mime_type=safe_mime,
                is_public=True,
                group_id=event.group_id,
                uploaded_by=uploaded_by,
                ip_address=ip_address,
                user_agent=user_agent,
                base_url=base_url,
                object_key=object_key,
            )
            image = await self.repo.create(
                event_id=event.id,
                media_id=media.id,
                url=media.url,
                thumb_url=media.thumb_url or media.url,
                position=position,
                uploaded_by=uploaded_by,
            )
            position += 1
            created.append(image)

        await self.session.flush()
        return created

    async def delete_image(self, event: Event, image: EventImage) -> None:
        """Remove one gallery image and free its slot."""
        media = await self.media_repo.get_by_id(image.media_id)
        await self.repo.delete(image)
        await self.session.flush()
        if media is not None:
            await self._discard_object(media.r2_object_key, media)

    async def delete_all_for_event(self, event: Event) -> None:
        """Drop every gallery image, used when the event itself is deleted."""
        images = await self.repo.list_for_event(event.id, limit=MAX_IMAGES_PER_EVENT)
        for image in images:
            media = await self.media_repo.get_by_id(image.media_id)
            await self.repo.delete(image)
            if media is not None:
                await self._discard_object(media.r2_object_key, media)
        await self.session.flush()

    async def _discard_object(self, object_key: str, media) -> None:
        """Delete a stored object and its tracking row, best effort.

        Storage being unreachable must not undo the change that was already
        committed to Postgres, so failures are logged rather than raised.
        """
        try:
            self.storage.delete_object(object_key)
        except Exception:
            logger.warning(
                "Could not delete media object %s", object_key, exc_info=True
            )
        if media is not None:
            await self.media_repo.delete(media)

    @staticmethod
    def _assert_within_limit(existing: int, incoming: int) -> None:
        if incoming <= 0:
            raise BadRequestException(
                "At least one image is required", "NO_IMAGES"
            )
        if incoming > MAX_IMAGES_PER_EVENT:
            raise BadRequestException(
                f"An event can hold at most {MAX_IMAGES_PER_EVENT} images",
                "TOO_MANY_IMAGES",
            )
        if existing + incoming > MAX_IMAGES_PER_EVENT:
            raise BadRequestException(
                f"An event can hold at most {MAX_IMAGES_PER_EVENT} images "
                f"({existing} already uploaded)",
                "TOO_MANY_IMAGES",
            )
