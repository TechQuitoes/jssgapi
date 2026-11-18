"""add Hindi translation (_hi) columns to members, groups, family and professionals

Revision ID: g1h2i3j4k5l6
Revises: f1e2d3c4b5a6
Create Date: 2026-11-18

Adds nullable *_hi columns to store pre-translated Hindi text so the Flutter
app can display Hindi content without calling the translation API at request
time.  All columns are nullable and completely independent of the existing
English columns, so this migration is safe to run on a live database and can
be rolled back without any data loss.

Each column is added only when it does not already exist so the migration
is idempotent and safe to re-run.

Tables affected:
  - members              : first_name_hi, middle_name_hi, last_name_hi,
                           address_line_hi, occupation_summary_hi
  - social_groups        : name_hi (may already exist), description_hi,
                           contact_name_hi
  - family_members       : name_hi, occupation_hi
  - professional_information : occupation_hi, company_name_hi, designation_hi
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "g1h2i3j4k5l6"
down_revision = "f1e2d3c4b5a6"
branch_labels = None
depends_on = None


def _add_if_not_exists(table: str, column_name: str, column_type: sa.types.TypeEngine) -> None:
    """Add *column_name* to *table* only when it does not already exist.

    Postgres does not support ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` in
    all versions targeted by this project, so we check
    ``information_schema.columns`` first and skip the DDL when the column is
    already there.  This keeps the migration idempotent.
    """
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :tbl AND column_name = :col"
        ),
        {"tbl": table, "col": column_name},
    )
    if result.fetchone() is None:
        op.add_column(table, sa.Column(column_name, column_type, nullable=True))


def upgrade() -> None:
    # ── members ──────────────────────────────────────────────────────────────
    _add_if_not_exists("members", "first_name_hi",         sa.String(200))
    _add_if_not_exists("members", "middle_name_hi",        sa.String(200))
    _add_if_not_exists("members", "last_name_hi",          sa.String(200))
    _add_if_not_exists("members", "address_line_hi",       sa.Text())
    _add_if_not_exists("members", "occupation_summary_hi", sa.Text())

    # ── social_groups ─────────────────────────────────────────────────────────
    _add_if_not_exists("social_groups", "name_hi",         sa.String(400))  # may already exist
    _add_if_not_exists("social_groups", "description_hi",  sa.Text())
    _add_if_not_exists("social_groups", "contact_name_hi", sa.String(400))

    # ── family_members ────────────────────────────────────────────────────────
    _add_if_not_exists("family_members", "name_hi",        sa.String(400))
    _add_if_not_exists("family_members", "occupation_hi",  sa.String(400))

    # ── professional_information ──────────────────────────────────────────────
    _add_if_not_exists("professional_information", "occupation_hi",   sa.String(400))
    _add_if_not_exists("professional_information", "company_name_hi", sa.String(400))
    _add_if_not_exists("professional_information", "designation_hi",  sa.String(400))


def downgrade() -> None:
    # professional_information
    op.drop_column("professional_information", "designation_hi")
    op.drop_column("professional_information", "company_name_hi")
    op.drop_column("professional_information", "occupation_hi")

    # family_members
    op.drop_column("family_members", "occupation_hi")
    op.drop_column("family_members", "name_hi")

    # social_groups  (name_hi was pre-existing — do NOT drop it in downgrade)
    op.drop_column("social_groups", "contact_name_hi")
    op.drop_column("social_groups", "description_hi")

    # members
    op.drop_column("members", "occupation_summary_hi")
    op.drop_column("members", "address_line_hi")
    op.drop_column("members", "last_name_hi")
    op.drop_column("members", "middle_name_hi")
    op.drop_column("members", "first_name_hi")
