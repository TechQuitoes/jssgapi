"""add guest role

Revision ID: i1j2k3l4m5n6
Revises: h1i2j3k4l5m6
Create Date: 2026-11-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "i1j2k3l4m5n6"
down_revision = "h1i2j3k4l5m6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO roles (id, name, description, created_at, updated_at)
            VALUES (gen_random_uuid(), 'GUEST', 'Guest user with limited view access', now(), now())
            ON CONFLICT (name) DO NOTHING;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM roles WHERE name = 'GUEST';"))
