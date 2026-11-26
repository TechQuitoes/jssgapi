"""add is_post_holder and post_holder_title to members

Revision ID: k1l2m3n4o5p6
Revises: j1k2l3m4n5o6
Create Date: 2026-11-27 00:20:00.000000

Adds explicit ``is_post_holder`` (boolean) and ``post_holder_title`` (string)
columns to ``members`` so group post holders (President, Secretary, Group Admin, etc.)
are managed separately from the member's community occupation/designation and user login roles.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "k1l2m3n4o5p6"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE members
                ADD COLUMN IF NOT EXISTS is_post_holder BOOLEAN NOT NULL DEFAULT false,
                ADD COLUMN IF NOT EXISTS post_holder_title VARCHAR(100);

            CREATE INDEX IF NOT EXISTS ix_members_is_post_holder ON members (is_post_holder);

            -- 1. Backfill known office bearers by profile_data->>'group_designation'
            UPDATE members
            SET is_post_holder = true,
                post_holder_title = CASE 
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) = 'president' THEN 'President'
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) = 'vice president' THEN 'Vice President'
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) = 'chairman' THEN 'Chairman'
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) = 'secretary' THEN 'Secretary'
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) = 'treasurer' THEN 'Treasurer'
                    WHEN LOWER(TRIM(profile_data->>'group_designation')) IN ('group admin', 'group_admin', 'admin') THEN 'Group Admin'
                    ELSE INITCAP(TRIM(profile_data->>'group_designation'))
                END
            WHERE profile_data->>'group_designation' IS NOT NULL
              AND LOWER(TRIM(profile_data->>'group_designation')) IN (
                  'president', 'vice president', 'chairman', 'secretary', 'treasurer', 'group admin', 'group_admin', 'admin'
              );

            -- 2. Backfill members who are Group Admins or Super Admins in users table
            UPDATE members m
            SET is_post_holder = true,
                post_holder_title = COALESCE(NULLIF(m.post_holder_title, ''), 'Group Admin')
            FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE m.user_id = u.id
              AND r.name IN ('GROUP_ADMIN', 'SUPER_ADMIN')
              AND (m.is_post_holder = false OR m.post_holder_title IS NULL);
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            DROP INDEX IF EXISTS ix_members_is_post_holder;
            ALTER TABLE members
                DROP COLUMN IF EXISTS post_holder_title,
                DROP COLUMN IF EXISTS is_post_holder;
            """
        )
    )
