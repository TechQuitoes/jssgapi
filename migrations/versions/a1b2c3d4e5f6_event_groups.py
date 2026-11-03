"""add event group selections

Revision ID: a1b2c3d4e5f6
Revises: e2f3a4b5c6d7
Create Date: 2026-11-03 00:00:00.000000

Events can belong to more than one social group. ``events.group_id`` stays as
the primary/legacy group for existing records, while ``event_groups`` holds the
complete selection. Existing grouped events are backfilled so their visibility
does not change when the API starts reading the association table.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "a1b2c3d4e5f6"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS event_groups (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                group_id UUID NOT NULL REFERENCES social_groups(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_by UUID,
                updated_by UUID,
                CONSTRAINT uq_event_groups_event_group UNIQUE (event_id, group_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_event_groups_event_id
                ON event_groups (event_id)
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_event_groups_group_id
                ON event_groups (group_id)
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO event_groups (event_id, group_id)
            SELECT id, group_id
            FROM events
            WHERE group_id IS NOT NULL
            ON CONFLICT (event_id, group_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(text("DROP TABLE IF EXISTS event_groups"))
