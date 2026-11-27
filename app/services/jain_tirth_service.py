from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jain_tirth import JainTirth
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import MIME_SUFFIX, validate_image_upload

logger = logging.getLogger(__name__)

COVER_OBJECT_KEY_TEMPLATE = "jain-tirths/{tirth_id}/cover/{unique}{suffix}"


class JainTirthService:
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
        tirth: JainTirth,
        *,
        file_name: str | None,
        content: bytes,
        mime_type: str | None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        safe_name, safe_mime = validate_image_upload(
            file_name, mime_type, content, error_code="INVALID_COVER_IMAGE"
        )
        object_key = COVER_OBJECT_KEY_TEMPLATE.format(
            tirth_id=tirth.id,
            unique=uuid.uuid4().hex,
            suffix=MIME_SUFFIX[safe_mime],
        )
        previous_key = (tirth.cover_image_object_key or "").strip() or None

        media = await MediaService(self.session, storage=self.storage).upload(
            owner_type="jain_tirth",
            owner_id=tirth.id,
            file_name=safe_name,
            content=content,
            mime_type=safe_mime,
            is_public=True,
            group_id=tirth.group_id,
            uploaded_by=uploaded_by,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
            object_key=object_key,
        )

        tirth.cover_image_url = media.url
        tirth.cover_image_thumb_url = media.thumb_url or media.url
        tirth.cover_image_object_key = object_key
        await self.session.flush()

        if previous_key and previous_key != object_key:
            await self._discard_object(previous_key)
        return media.url

    async def remove_cover(self, tirth: JainTirth) -> None:
        object_key = (tirth.cover_image_object_key or "").strip() or None
        tirth.cover_image_url = None
        tirth.cover_image_thumb_url = None
        tirth.cover_image_object_key = None
        await self.session.flush()
        if object_key:
            await self._discard_object(object_key)

    async def _discard_object(self, object_key: str) -> None:
        try:
            self.storage.delete_object(object_key)
        except Exception:
            logger.warning(
                "Could not delete media object %s", object_key, exc_info=True
            )
        media = await self.media_repo.get_by_object_key(object_key)
        if media is not None:
            await self.media_repo.delete(media)
