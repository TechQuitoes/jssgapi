"""add image thumbnails and WebP support

Revision ID: l1m2n3o4p5q6
Revises: k1l2m3n4o5p6
Create Date: 2026-11-27 15:58:00.000000

Adds thumbnail URL and storage key columns across media and image-bearing tables:
- ``media.thumb_url`` and ``media.thumb_object_key``
- ``event_images.thumb_url``
- ``events.cover_image_thumb_url``
- ``jain_tirth_images.thumb_url``
- ``jain_tirths.cover_image_thumb_url``
- ``advertisements.cover_image_thumb_url``
- ``members.profile_photo_thumb_url``

Existing rows are backfilled so ``thumb_url`` defaults to the existing full-size URL.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "l1m2n3o4p5q6"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            -- 1. media table
            ALTER TABLE media
                ADD COLUMN IF NOT EXISTS thumb_url TEXT,
                ADD COLUMN IF NOT EXISTS thumb_object_key VARCHAR(500);

            -- 2. event_images table
            ALTER TABLE event_images
                ADD COLUMN IF NOT EXISTS thumb_url TEXT;

            -- 3. events table
            ALTER TABLE events
                ADD COLUMN IF NOT EXISTS cover_image_thumb_url TEXT;

            -- 4. jain_tirth_images table
            ALTER TABLE jain_tirth_images
                ADD COLUMN IF NOT EXISTS thumb_url TEXT;

            -- 5. jain_tirths table
            ALTER TABLE jain_tirths
                ADD COLUMN IF NOT EXISTS cover_image_thumb_url TEXT;

            -- 6. advertisements table
            ALTER TABLE advertisements
                ADD COLUMN IF NOT EXISTS cover_image_thumb_url TEXT;

            -- 7. members table
            ALTER TABLE members
                ADD COLUMN IF NOT EXISTS profile_photo_thumb_url TEXT;

            -- Backfill existing rows so thumb_url points to the existing URL
            UPDATE media SET thumb_url = url WHERE thumb_url IS NULL;
            UPDATE event_images SET thumb_url = url WHERE thumb_url IS NULL;
            UPDATE events SET cover_image_thumb_url = cover_image_url WHERE cover_image_thumb_url IS NULL AND cover_image_url IS NOT NULL;
            UPDATE jain_tirth_images SET thumb_url = url WHERE thumb_url IS NULL;
            UPDATE jain_tirths SET cover_image_thumb_url = cover_image_url WHERE cover_image_thumb_url IS NULL AND cover_image_url IS NOT NULL;
            UPDATE advertisements SET cover_image_thumb_url = cover_image_url WHERE cover_image_thumb_url IS NULL AND cover_image_url IS NOT NULL;
            UPDATE members SET profile_photo_thumb_url = profile_photo_url WHERE profile_photo_thumb_url IS NULL AND profile_photo_url IS NOT NULL;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE members DROP COLUMN IF EXISTS profile_photo_thumb_url;
            ALTER TABLE advertisements DROP COLUMN IF EXISTS cover_image_thumb_url;
            ALTER TABLE jain_tirths DROP COLUMN IF EXISTS cover_image_thumb_url;
            ALTER TABLE jain_tirth_images DROP COLUMN IF EXISTS thumb_url;
            ALTER TABLE events DROP COLUMN IF EXISTS cover_image_thumb_url;
            ALTER TABLE event_images DROP COLUMN IF EXISTS thumb_url;
            ALTER TABLE media DROP COLUMN IF EXISTS thumb_object_key;
            ALTER TABLE media DROP COLUMN IF EXISTS thumb_url;
            """
        )
    )
