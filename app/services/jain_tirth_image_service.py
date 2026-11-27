from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestException, NotFoundException
from app.models.jain_tirth import JainTirth
from app.models.jain_tirth_image import JainTirthImage
from app.repositories.jain_tirth_image_repository import (
    JainTirthImageRepository,
)
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import MIME_SUFFIX, validate_image_upload

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_TIRTH = 10

IMAGE_OBJECT_KEY_TEMPLATE = "jain-tirths/{tirth_id}/gallery/{unique}{suffix}"


@dataclass(frozen=True)
class ImageUpload:
    file_name: str | None
    content: bytes
    mime_type: str | None


class JainTirthImageService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.storage = storage or get_storage()
        self.repo = JainTirthImageRepository(session)
        self.media_repo = MediaRepository(session)

    async def list_images(self, tirth: JainTirth) -> list[JainTirthImage]:
        return await self.repo.list_for_tirth(tirth.id)

    async def find_image(
        self, tirth: JainTirth, image_id: uuid.UUID
    ) -> JainTirthImage:
        image = await self.repo.get_by_id(image_id)
        if image is None or image.jain_tirth_id != tirth.id:
            raise NotFoundException("Jain Tirth image", str(image_id))
        return image

    async def count_images(self, tirth: JainTirth) -> int:
        return await self.repo.count_for_tirth(tirth.id)

    async def batch_count_images(
        self, tirths: list[JainTirth]
    ) -> dict[uuid.UUID, int]:
        return await self.repo.batch_count_for_tirths([t.id for t in tirths])

    async def upload_images(
        self,
        tirth: JainTirth,
        uploads: list[ImageUpload],
        *,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> list[JainTirthImage]:
        existing = await self.count_images(tirth)
        self._assert_within_limit(existing, len(uploads))

        media_service = MediaService(self.session, storage=self.storage)
        position = existing
        created: list[JainTirthImage] = []
        for upload in uploads:
            safe_name, safe_mime = validate_image_upload(
                upload.file_name,
                upload.mime_type,
                upload.content,
                error_code="INVALID_TIRTH_IMAGE",
            )
            object_key = IMAGE_OBJECT_KEY_TEMPLATE.format(
                tirth_id=tirth.id,
                unique=uuid.uuid4().hex,
                suffix=MIME_SUFFIX[safe_mime],
            )
            media = await media_service.upload(
                owner_type="jain_tirth",
                owner_id=tirth.id,
                file_name=safe_name,
                content=upload.content,
                mime_type=safe_mime,
                is_public=True,
                group_id=tirth.group_id,
                uploaded_by=uploaded_by,
                ip_address=ip_address,
                user_agent=user_agent,
                base_url=base_url,
                object_key=object_key,
            )
            image = await self.repo.create(
                jain_tirth_id=tirth.id,
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

    async def delete_image(
        self, tirth: JainTirth, image: JainTirthImage
    ) -> None:
        media = await self.media_repo.get_by_id(image.media_id)
        await self.repo.delete(image)
        await self.session.flush()
        if media is not None:
            await self._discard_object(media.r2_object_key, media)

    async def delete_all_for_tirth(self, tirth: JainTirth) -> None:
        images = await self.repo.list_for_tirth(
            tirth.id, limit=MAX_IMAGES_PER_TIRTH
        )
        for image in images:
            media = await self.media_repo.get_by_id(image.media_id)
            await self.repo.delete(image)
            if media is not None:
                await self._discard_object(media.r2_object_key, media)
        await self.session.flush()

    async def _discard_object(self, object_key: str, media) -> None:
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
        if incoming > MAX_IMAGES_PER_TIRTH:
            raise BadRequestException(
                f"A Jain Tirth can hold at most {MAX_IMAGES_PER_TIRTH} images",
                "TOO_MANY_IMAGES",
            )
        if existing + incoming > MAX_IMAGES_PER_TIRTH:
            raise BadRequestException(
                f"A Jain Tirth can hold at most {MAX_IMAGES_PER_TIRTH} images "
                f"({existing} already uploaded)",
                "TOO_MANY_IMAGES",
            )
