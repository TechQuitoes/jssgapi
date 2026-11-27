"""Copy member photos from Google Drive into Cloudflare R2.

The member directory was seeded from a Google Sheet whose photo column holds
``drive.google.com`` share links. Those links cannot be rendered by the mobile
app: an unshared file resolves to an ``accounts.google.com`` sign-in page, and a
shared one resolves to Drive's HTML preview. ``Image.network`` gets neither.

This service fetches the bytes once and stores them under the same project
storage that serves uploads, then points the member's photo fields at a URL that
returns ``image/*`` with no cookies and no headers.

Design notes:

* **Nothing in Drive changes.** No file is shared, moved, renamed or deleted.
* **The original link is kept** in ``*_photo_drive_url`` so a photo stays
  re-syncable from the sheet, and so a changed sheet link is detected.
* **Idempotent.** A photo whose source has not changed is migrated at most once;
  re-running reports it as ``already_migrated`` and re-uploads nothing.
* **Changed links replace the photo.** When the sheet points at a different
  Drive file than the one last migrated, the new bytes are downloaded, the
  stored object is superseded, and the older object is deleted.
* **One failure never stops the run.** Every member is migrated in its own
  nested transaction, and an unreadable Drive file is recorded and skipped.
* **Reuses the existing storage backend** - there is no second R2 client, and
  no new upload path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AuditAction, MediaType
from app.core.exceptions import ServiceUnavailableException
from app.models.media import Media
from app.models.member import Member
from app.repositories.media_repository import MediaRepository
from app.services.audit_service import AuditService
from app.services.google_drive_photos import (
    DrivePhotoDownloader,
    DrivePhotoError,
    PhotoLinkKind,
    is_direct_image_url,
    parse_photo_link,
)
from app.services.media_service import build_media_url
from app.services.media_storage import StorageBackend, get_storage

logger = logging.getLogger(__name__)

#: Deterministic object key prefix per member and photo slot. The extension is
#: added once the bytes have been inspected, so the prefix is what makes the
#: migration idempotent even across runs.
OBJECT_KEY_PREFIX = "members/{member_id}/photos/{role}"

#: Photos are read and copied one at a time; a serverless invocation has a wall
#: clock, so a single call is bounded rather than allowed to walk the directory.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

MIGRATED = "migrated"
ALREADY_MIGRATED = "already_migrated"
REPLACED = "replaced"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class PhotoField:
    """The column and JSONB entries backing one photo slot on a member."""

    role: str
    drive_attr: str
    key_attr: str
    url_attr: str
    json_key: str
    mirror_to_profile_photo_url: bool


PHOTO_FIELDS: tuple[PhotoField, ...] = (
    PhotoField(
        role="member",
        drive_attr="profile_photo_drive_url",
        key_attr="profile_photo_r2_object_key",
        url_attr="profile_photo_r2_url",
        json_key="member_photo_link",
        mirror_to_profile_photo_url=True,
    ),
    PhotoField(
        role="spouse",
        drive_attr="spouse_photo_drive_url",
        key_attr="spouse_photo_r2_object_key",
        url_attr="spouse_photo_r2_url",
        json_key="spouse_photo_link",
        mirror_to_profile_photo_url=False,
    ),
)


@dataclass
class PhotoOutcome:
    member_id: str
    role: str
    status: str
    reason: str | None = None
    source_url: str | None = None
    object_key: str | None = None
    r2_url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None


@dataclass
class MigrationSummary:
    dry_run: bool = False
    scanned: int = 0
    migrated: int = 0
    already_migrated: int = 0
    replaced: int = 0
    skipped: int = 0
    failed: int = 0
    outcomes: list[PhotoOutcome] = field(default_factory=list)
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "migrated": self.migrated,
            "already_migrated": self.already_migrated,
            "replaced": self.replaced,
            "skipped": self.skipped,
            "failed": self.failed,
            "has_more": self.has_more,
            "failures": [asdict(o) for o in self.outcomes if o.status == FAILED],
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def _member_source_link(member: Member, field_: PhotoField) -> str:
    """The Drive link to copy from, for this slot.

    ``profile_data`` wins when it holds a Drive link, because the read path in
    ``profile_service`` overlays it on top of ``profile_photo_url`` - so it is
    the link the app is actually being served.

    Once a photo is migrated those fields hold the R2 URL instead, so the
    preserved ``*_drive_url`` is consulted next. That is what lets a photo whose
    stored object later disappears be copied again.
    """
    candidates: list[str] = []
    profile_data = member.profile_data or {}
    json_value = profile_data.get(field_.json_key)
    if isinstance(json_value, str):
        candidates.append(json_value.strip())
    if field_.mirror_to_profile_photo_url:
        candidates.append((member.profile_photo_url or "").strip())
    candidates.append((getattr(member, field_.drive_attr) or "").strip())

    for candidate in candidates:
        if parse_photo_link(candidate).kind is PhotoLinkKind.FILE_ID:
            return candidate
    return candidates[0] if candidates else ""


def _file_id(raw_url: str | None) -> str | None:
    """The Drive file id in ``raw_url``, or ``None`` for anything else."""
    return parse_photo_link(raw_url).file_id


class DrivePhotoMigration:
    """Runs the Drive to R2 photo migration over members."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: StorageBackend | None = None,
        downloader: DrivePhotoDownloader | None = None,
        base_url: str | None = None,
    ) -> None:
        self.session = session
        self.storage = storage or get_storage()
        self.downloader = downloader or DrivePhotoDownloader()
        self.media_repo = MediaRepository(session)
        self.audit = AuditService(session)
        self.base_url = base_url

    # ------------------------------------------------------------------
    async def pending_members(self, offset: int, limit: int) -> list[Member]:
        """Members with a photo that still needs work.

        Two cases qualify a member:

        * a slot whose ``*_r2_object_key`` is not recorded yet (a first pass);
        * a slot whose stored link now looks like a Drive link even though the
          object key is set - the sheet link changed, so the photo must be
          replaced.

        The second case is why a fully migrated member is not simply skipped:
        its ``profile_data`` / ``profile_photo_url`` is rewritten to the R2 URL
        after a migration, so a Drive link reappearing there means the sheet
        supplied a new one.
        """
        profile_photo_url_text = Member.profile_photo_url

        def slot_links(field_: PhotoField) -> list:
            links = [Member.profile_data[field_.json_key].as_string()]
            if field_.mirror_to_profile_photo_url:
                links.append(profile_photo_url_text)
            return links

        def looks_like_drive(link) -> Any:
            return or_(
                link.ilike("%drive.google.com%"),
                link.ilike("%docs.google.com%"),
            )

        text_links = [
            link for field_ in PHOTO_FIELDS for link in slot_links(field_)
        ]
        has_link = or_(
            *[link.isnot(None) for link in text_links],
            profile_photo_url_text.isnot(None),
            profile_photo_url_text != "",
        )

        key_columns = [getattr(Member, field_.key_attr) for field_ in PHOTO_FIELDS]
        not_migrated = or_(
            *[column.is_(None) for column in key_columns],
            *[column == "" for column in key_columns],
        )

        changed = or_(
            *[
                and_(
                    key_column.isnot(None),
                    key_column != "",
                    or_(*[looks_like_drive(link) for link in slot_links(field_)]),
                )
                for field_, key_column in zip(PHOTO_FIELDS, key_columns, strict=True)
            ]
        )

        stmt = (
            select(Member)
            .where(has_link, or_(not_migrated, changed))
            .order_by(Member.id)
            .offset(offset)
            .limit(limit + 1)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique())

    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        dry_run: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        member_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> MigrationSummary:
        summary = MigrationSummary(dry_run=dry_run)

        if not dry_run and not self.storage.available:
            raise ServiceUnavailableException(
                self.storage.unavailable_message, "MEDIA_STORAGE_UNAVAILABLE"
            )

        members = (
            [m for m in [await self._member(member_id)] if m is not None]
            if member_id
            else await self.pending_members(offset, min(limit, MAX_LIMIT))
        )
        if len(members) > limit:
            summary.has_more = True
            members = members[:limit]

        for member in members:
            summary.scanned += 1
            # Each member is its own transaction: a bad photo or a failed write
            # is rolled back on its own and the remaining members still commit.
            async with self.session.begin_nested():
                for field_ in PHOTO_FIELDS:
                    outcome = await self._migrate_one(member, field_, dry_run=dry_run)
                    if outcome is None:
                        continue
                    summary.outcomes.append(outcome)
                    setattr(
                        summary, outcome.status, getattr(summary, outcome.status) + 1
                    )

        if not dry_run:
            await self.session.flush()
            await self.audit.log(
                AuditAction.MEDIA_MIGRATE,
                user_id=user_id,
                entity_type="member",
                details={
                    "scanned": summary.scanned,
                    "migrated": summary.migrated,
                    "already_migrated": summary.already_migrated,
                    "skipped": summary.skipped,
                    "failed": summary.failed,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
        return summary

    async def _member(self, member_id: uuid.UUID) -> Member | None:
        return await self.session.get(Member, member_id)

    # ------------------------------------------------------------------
    async def _migrate_one(
        self, member: Member, field_: PhotoField, *, dry_run: bool
    ) -> PhotoOutcome | None:
        member_id = str(member.id)
        source_url = _member_source_link(member, field_)
        recorded_key = (getattr(member, field_.key_attr) or "").strip()
        recorded_url = (getattr(member, field_.url_attr) or "").strip()
        recorded_source = (getattr(member, field_.drive_attr) or "").strip()

        def outcome(
            status: str,
            reason: str | None = None,
            **extra: Any,
        ) -> PhotoOutcome:
            return PhotoOutcome(
                member_id=member_id,
                role=field_.role,
                status=status,
                reason=reason,
                source_url=source_url or None,
                **extra,
            )

        # A photo is "changed" when the sheet now points at a different Drive
        # file than the one last migrated. ``*_photo_drive_url`` preserves the
        # source of the last successful migration, so comparing file ids tells a
        # genuine edit in the sheet from a re-run of the same row.
        source_file_id = _file_id(source_url)
        recorded_file_id = _file_id(recorded_source)
        replaced = bool(
            recorded_key
            and source_file_id
            and recorded_file_id
            and source_file_id != recorded_file_id
        )

        # The recorded key is checked before the link is classified. After a
        # migration the stored link is the R2 URL, not the Drive link, so
        # classifying first would report a finished photo as "not a Drive
        # link" and hide whether its bytes are actually there.
        if recorded_key and not replaced:
            if await asyncio.to_thread(self.storage.object_exists, recorded_key):
                return outcome(
                    ALREADY_MIGRATED,
                    "already migrated",
                    object_key=recorded_key,
                    r2_url=recorded_url or self._public_url(recorded_key),
                )
            # The row claims a migration but the bytes are gone; re-copy them.
            logger.warning(
                "recorded R2 object %s for member %s %s is gone; re-migrating",
                recorded_key,
                member_id,
                field_.role,
            )
            recorded_key = ""
            recorded_url = ""

        if not source_url:
            # Nothing stored for this slot. Not worth reporting against every
            # member with no photo.
            return None

        if is_direct_image_url(source_url):
            return outcome(
                SKIPPED,
                "already a direct image URL that serves bytes unauthenticated",
            )

        link = parse_photo_link(source_url)
        if link.kind is not PhotoLinkKind.FILE_ID:
            return outcome(SKIPPED, "not a Google Drive file link")

        if not replaced:
            # Same source as before: reuse the object already in the bucket
            # instead of downloading it again.
            existing = recorded_key or await asyncio.to_thread(
                self.storage.find_object_key, self._prefix(member, field_)
            )
            if existing:
                r2_url = await self._finish(
                    member,
                    field_,
                    object_key=existing,
                    r2_url=recorded_url or self._public_url(existing),
                    source_url=source_url,
                    dry_run=dry_run,
                )
                return outcome(
                    ALREADY_MIGRATED,
                    "object already present in storage",
                    object_key=existing,
                    r2_url=r2_url,
                )

        if dry_run:
            if replaced:
                return outcome(
                    REPLACED,
                    "would download the changed sheet photo and replace the "
                    "stored one",
                )
            return outcome(MIGRATED, "would download from Drive and store in R2")

        try:
            photo = await asyncio.to_thread(self.downloader.download, link, field_.role)
        except DrivePhotoError as exc:
            return outcome(FAILED, exc.reason)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "photo download failed for member %s %s",
                member_id,
                field_.role,
                exc_info=True,
            )
            return outcome(FAILED, f"unexpected download error: {exc}")

        object_key = self._object_key(member, field_, photo.file_name)
        await asyncio.to_thread(
            self.storage.upload_bytes,
            object_key,
            photo.content,
            photo.mime_type,
        )

        await self._record_media(
            member,
            field_,
            object_key=object_key,
            file_name=photo.file_name,
            mime_type=photo.mime_type,
            size=len(photo.content),
        )

        # The new object is durable and referenced, so the superseded one can
        # go. A different extension means a different object key, so the old
        # key would otherwise linger as an orphan.
        if replaced and recorded_key and recorded_key != object_key:
            await self._discard_object(recorded_key)

        r2_url = await self._finish(
            member,
            field_,
            object_key=object_key,
            r2_url=self._public_url(object_key),
            source_url=source_url,
            dry_run=dry_run,
        )
        return outcome(
            REPLACED if replaced else MIGRATED,
            object_key=object_key,
            r2_url=r2_url,
            file_name=photo.file_name,
            mime_type=photo.mime_type,
        )

    async def _discard_object(self, object_key: str) -> None:
        """Delete a superseded object and its ``media`` row, best effort.

        The replacement has already been committed to the member, so a storage
        or database failure here must not undo it; the failure is logged and
        the run continues.
        """
        try:
            await asyncio.to_thread(self.storage.delete_object, object_key)
        except Exception:  # noqa: BLE001 - deletion is best effort
            logger.warning(
                "could not delete superseded R2 object %s", object_key, exc_info=True
            )
        media = await self.media_repo.get_by_object_key(object_key)
        if media is not None:
            await self.media_repo.delete(media)

    # ------------------------------------------------------------------
    def _prefix(self, member: Member, field_: PhotoField) -> str:
        return OBJECT_KEY_PREFIX.format(member_id=member.id, role=field_.role)

    def _object_key(self, member: Member, field_: PhotoField, file_name: str) -> str:
        suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "img"
        suffix = "".join(ch for ch in suffix if ch.isalnum()) or "img"
        return f"{self._prefix(member, field_)}.{suffix}"

    def _public_url(self, object_key: str) -> str:
        if self.storage.serves_public_urls:
            public_url = getattr(self.storage, "public_url", "")
            return f"{public_url.rstrip('/')}/{object_key}"
        return ""

    async def _record_media(
        self,
        member: Member,
        field_: PhotoField,
        *,
        object_key: str,
        file_name: str,
        mime_type: str,
        size: int,
    ) -> None:
        """Track the object in ``media`` so the normal route can serve it.

        The object key is unique, so an earlier row for the same photo is
        reused rather than duplicated.
        """
        existing = await self.media_repo.get_by_object_key(object_key)
        public_url = self._public_url(object_key)
        if existing is not None:
            existing.url = public_url or existing.url
            existing.file_size = size
            existing.mime_type = mime_type
            existing.file_name = file_name
            return

        media = Media(
            owner_type="member",
            owner_id=member.id,
            file_name=file_name,
            file_type=MediaType.IMAGE.value,
            mime_type=mime_type,
            file_size=size,
            r2_object_key=object_key,
            url=public_url,
            is_public=True,
            uploaded_by=None,
        )
        self.session.add(media)
        await self.session.flush()
        if not public_url:
            # No public bucket domain, so the photo is served by the media
            # content route instead.
            media.url = build_media_url(media.id, self.base_url)

    async def _finish(
        self,
        member: Member,
        field_: PhotoField,
        *,
        object_key: str,
        r2_url: str,
        source_url: str,
        dry_run: bool,
    ) -> str:
        """Write the migrated link onto the member.

        The original Drive link is preserved in ``*_photo_drive_url``; the
        serving URL is written both to the dedicated column and to the field
        the app reads (``profile_photo_url`` / ``profile_data``).
        """
        if dry_run:
            return r2_url

        setattr(member, field_.key_attr, object_key)
        # Always preserve the migrated source, even when the backend has no
        # public URL to serve from: it is what a later run compares against to
        # detect a changed sheet photo.
        setattr(member, field_.drive_attr, source_url)
        if r2_url:
            setattr(member, field_.url_attr, r2_url)
            profile_data = dict(member.profile_data or {})
            profile_data[field_.json_key] = r2_url
            member.profile_data = profile_data
            if field_.mirror_to_profile_photo_url:
                member.profile_photo_url = r2_url
                member.profile_photo_thumb_url = r2_url
        return r2_url
