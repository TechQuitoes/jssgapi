"""Member and spouse photo slots.

The bytes live in object storage (Cloudflare R2 in production); Postgres only
holds the URL, the object key and the ``media`` row that tracks the object.
Each slot is written through everywhere a photo is already represented:

* ``members.profile_photo_url`` - the directory column the panel renders.
* ``members.<role>_photo_r2_url`` / ``members.<role>_photo_r2_object_key`` -
  the migration/repair bookkeeping columns the Drive migration uses.
* ``members.profile_data['member_photo_link' | 'spouse_photo_link']`` - the
  profile field ``GET /api/v1/profile`` returns.

Replacement uploads the new object first and only then overwrites the row, so
a storage failure can never leave a member pointing at an object that does not
exist. The previous object is deleted afterwards, best effort.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestException
from app.models.member import Member
from app.repositories.media_repository import MediaRepository
from app.services.media_service import MediaService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import (
    MIME_SUFFIX,
    safe_file_name,
    validate_image_upload,
)

logger = logging.getLogger(__name__)

#: Object key template. One predictable prefix per member and role keeps the
#: Drive migration (``members/{id}/photos/{role}``) distinct, so re-running it
#: never claims an object uploaded through the API.
OBJECT_KEY_TEMPLATE = "members/{member_id}/{role}/{unique}{suffix}"

_MIME_SUFFIX = MIME_SUFFIX


@dataclass(frozen=True)
class PhotoSlot:
    """Where one of a member's two photos is recorded."""

    role: str
    key_column: str
    url_column: str
    profile_key: str
    display_column: str | None


MEMBER_PHOTO = PhotoSlot(
    role="member",
    key_column="profile_photo_r2_object_key",
    url_column="profile_photo_r2_url",
    profile_key="member_photo_link",
    display_column="profile_photo_url",
)
SPOUSE_PHOTO = PhotoSlot(
    role="spouse",
    key_column="spouse_photo_r2_object_key",
    url_column="spouse_photo_r2_url",
    profile_key="spouse_photo_link",
    display_column=None,
)

SLOTS: dict[str, PhotoSlot] = {
    MEMBER_PHOTO.role: MEMBER_PHOTO,
    SPOUSE_PHOTO.role: SPOUSE_PHOTO,
}


def safe_photo_name(file_name: str | None) -> str:
    """A file name with any client-supplied path stripped, capped at 255."""
    return safe_file_name(file_name, fallback="photo")


def validate_photo(file_name: str | None, mime_type: str | None, content: bytes):
    """Check the upload and return ``(file_name, mime_type)`` for storage.

    The bytes decide what the file is: the declared content type and the
    extension are only checked for a declared image type that disagrees with
    them, and the stored name always carries an extension the media layer
    accepts.
    """
    return validate_image_upload(
        file_name, mime_type, content, error_code="INVALID_PHOTO"
    )


class MemberPhotoService:
    def __init__(self, session: AsyncSession, storage: StorageBackend | None = None):
        self.session = session
        self.storage = storage or get_storage()
        self.media_repo = MediaRepository(session)

    async def upload(
        self,
        member: Member,
        *,
        role: str,
        file_name: str | None,
        content: bytes,
        mime_type: str | None,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> str:
        """Store a photo for ``member`` and return the URL to serve it from."""
        slot = self._slot(role)
        safe_name, safe_mime = validate_photo(file_name, mime_type, content)
        object_key = OBJECT_KEY_TEMPLATE.format(
            member_id=member.id,
            role=slot.role,
            unique=uuid.uuid4().hex,
            suffix=_MIME_SUFFIX[safe_mime],
        )
        previous_key = (getattr(member, slot.key_column) or "").strip() or None

        media = await MediaService(self.session, storage=self.storage).upload(
            owner_type="member",
            owner_id=member.id,
            file_name=safe_name,
            content=content,
            mime_type=safe_mime,
            is_public=True,
            group_id=member.group_id,
            uploaded_by=uploaded_by,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=base_url,
            object_key=object_key,
        )

        self._write_slot(
            member,
            slot,
            media.url,
            object_key,
            thumb_url=media.thumb_url or media.url,
        )
        await self.session.flush()

        # The new object is durable and referenced, so the old one can go. An
        # external (Drive) URL has no object of ours to delete.
        if previous_key and previous_key != object_key:
            await self._discard_object(previous_key)
        return media.url

    async def remove(self, member: Member, *, role: str) -> None:
        """Drop one photo: clear the recorded URL, then delete the object."""
        slot = self._slot(role)
        object_key = (getattr(member, slot.key_column) or "").strip() or None
        self._write_slot(member, slot, None, None)
        if slot.role == "member":
            member.profile_photo_thumb_url = None
        await self.session.flush()
        if object_key:
            await self._discard_object(object_key)

    async def remove_all(self, member: Member) -> None:
        """Drop every photo slot, used when the member itself is deleted."""
        for slot in SLOTS.values():
            await self.remove(member, role=slot.role)

    def _write_slot(
        self,
        member: Member,
        slot: PhotoSlot,
        url: str | None,
        object_key: str | None,
        thumb_url: str | None = None,
    ) -> None:
        setattr(member, slot.key_column, object_key)
        # Only an absolute URL is a real public object URL; the relative media
        # path is not one and must not be recorded as such.
        is_absolute = url is not None and (
            url.startswith("https://") or url.startswith("http://")
        )
        setattr(member, slot.url_column, url if is_absolute else None)
        if slot.display_column:
            setattr(member, slot.display_column, url)
            if slot.role == "member":
                member.profile_photo_thumb_url = thumb_url or (url if url else None)
        profile_data = dict(member.profile_data or {})
        profile_data[slot.profile_key] = url
        if slot.role == "member":
            profile_data["member_photo_thumb_link"] = thumb_url or url
        member.profile_data = profile_data

    async def _discard_object(self, object_key: str) -> None:
        """Delete the stored object and its tracking row.

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

    @staticmethod
    def _slot(role: str) -> PhotoSlot:
        try:
            return SLOTS[role]
        except KeyError:
            raise BadRequestException(
                f"Unknown photo slot '{role}'", "INVALID_PHOTO"
            ) from None
