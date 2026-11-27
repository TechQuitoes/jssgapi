from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, MediaType
from app.core.exceptions import BadRequestException
from app.models.media import Media
from app.repositories.media_repository import MediaRepository
from app.services.audit_service import AuditService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import (
    build_object_key,
    validate_extension,
    validate_file_size,
    validate_mime_type,
)
from app.utils.image_optimizer import process_image_upload

logger = logging.getLogger(__name__)
settings = get_settings()

#: Path that actually streams the bytes, relative to the API root.
MEDIA_CONTENT_PATH = "/api/v1/media/{media_id}/content"


def build_media_url(
    media_id: uuid.UUID,
    base_url: str | None = None,
    variant: str | None = None,
) -> str:
    """Absolute URL the API itself serves media from.

    ``base_url`` normally comes from the incoming request, so the URL stays
    correct on any deployment domain. Falls back to ``PUBLIC_BASE_URL`` and
    finally to a relative path.
    """
    path = MEDIA_CONTENT_PATH.format(media_id=media_id)
    if variant:
        path = f"{path}?variant={variant}"
    base = (base_url or settings.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def _is_absolute(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


class MediaService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.repo = MediaRepository(session)
        self.storage = storage or get_storage()
        self.audit = AuditService(session)

    async def upload(
        self,
        *,
        owner_type: str,
        owner_id: uuid.UUID,
        file_name: str,
        content: bytes,
        mime_type: str,
        width: int | None = None,
        height: int | None = None,
        is_public: bool = True,
        group_id: uuid.UUID | None = None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
        object_key: str | None = None,
    ) -> Media:
        category = validate_mime_type(mime_type)
        validate_extension(file_name, category)
        validate_file_size(len(content), category)
        if len(content) == 0:
            raise BadRequestException("Uploaded file is empty")

        file_type = {
            "image": MediaType.IMAGE.value,
            "video": MediaType.VIDEO.value,
            "document": MediaType.DOCUMENT.value,
        }[category]

        # A caller may pin the key (member photos keep one predictable path per
        # member and role); otherwise the key is generated as before. The key is
        # still validated by the storage backend, so a pinned key cannot escape
        # the bucket root.
        object_key = object_key or build_object_key(category, file_name, owner_id)

        thumb_object_key: str | None = None
        thumb_url: str | None = None
        media_id = uuid.uuid4()

        if category == "image":
            processed = process_image_upload(content, mime_type)
            content = processed.full.content
            mime_type = processed.full.mime_type
            if processed.full.width and processed.full.height:
                width = processed.full.width
                height = processed.full.height

            if processed.thumb is not None:
                p = Path(object_key)
                thumb_object_key = f"{p.parent}/thumb_{p.stem}.webp"
                thumb_storage_url = self.storage.upload_bytes(
                    thumb_object_key,
                    processed.thumb.content,
                    processed.thumb.mime_type,
                )
                thumb_url = (
                    thumb_storage_url
                    if _is_absolute(thumb_storage_url)
                    else build_media_url(media_id, base_url, variant="thumb")
                )

        # Raises rather than silently dropping the bytes, so a failed upload can
        # never be reported as a success with an unusable URL.
        storage_url = self.storage.upload_bytes(object_key, content, mime_type)

        media_url = (
            storage_url
            if _is_absolute(storage_url)
            else build_media_url(media_id, base_url)
        )

        media = await self.repo.create(
            id=media_id,
            owner_type=owner_type,
            owner_id=owner_id,
            group_id=group_id,
            file_name=file_name,
            file_type=file_type,
            mime_type=mime_type,
            file_size=len(content),
            r2_object_key=object_key,
            thumb_object_key=thumb_object_key,
            url=media_url,
            thumb_url=thumb_url,
            width=width,
            height=height,
            is_public=is_public,
            uploaded_by=uploaded_by,
        )

        await self.audit.log(
            AuditAction.MEDIA_UPLOAD,
            user_id=uploaded_by,
            entity_type=owner_type,
            entity_id=owner_id,
            details={"file_name": file_name, "file_size": len(content)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        # created_at/updated_at are server defaults, so they are expired after
        # the flush and reading them would trigger an implicit lazy load - which
        # cannot be awaited in async and raises MissingGreenlet. The flush above
        # has already persisted the url we set, so refreshing is safe.
        await self.session.refresh(media)
        return media

    def read(self, media: Media) -> bytes:
        return self.storage.read_bytes(media.r2_object_key)

    def read_thumb(self, media: Media) -> bytes | None:
        if not media.thumb_object_key:
            return None
        try:
            return self.storage.read_bytes(media.thumb_object_key)
        except Exception:
            logger.warning(
                "Could not read thumbnail for media %s", media.id, exc_info=True
            )
            return None

    async def delete(
        self,
        media: Media,
        *,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.storage.delete_object(media.r2_object_key)
        if media.thumb_object_key:
            self.storage.delete_object(media.thumb_object_key)
        await self.repo.delete(media)
        await self.audit.log(
            AuditAction.MEDIA_DELETE,
            user_id=user_id,
            entity_type=media.owner_type,
            entity_id=media.owner_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
