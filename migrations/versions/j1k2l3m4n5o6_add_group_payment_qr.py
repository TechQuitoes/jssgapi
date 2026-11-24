"""add payment QR image columns to social_groups

Revision ID: j1k2l3m4n5o6
Revises: i1j2k3l4m5n6
Create Date: 2026-11-24 14:00:00.000000

Adds ``payment_qr_url`` and ``payment_qr_object_key`` to ``social_groups``
so each group can store a UPI / bank payment QR code image that members
can scan to make payments.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "j1k2l3m4n5o6"
down_revision = "i1j2k3l4m5n6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE social_groups
                ADD COLUMN IF NOT EXISTS payment_qr_url TEXT,
                ADD COLUMN IF NOT EXISTS payment_qr_object_key VARCHAR(500)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE social_groups
                DROP COLUMN IF EXISTS payment_qr_object_key,
                DROP COLUMN IF EXISTS payment_qr_url
            """
        )
    )
