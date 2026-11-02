"""add database-backed advertisement subscription pricing and purchase snapshots

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-11-02 00:00:00.000000

The existing advertisement form accepted an arbitrary amount and the duration
options were hardcoded in the API. This migration makes pricing persistent:

* ``advertisement_subscription_plans`` stores the three sellable plans.
* ``advertisements`` gains the snapshot columns that keep an existing purchase
  priced at what was actually paid, even after a Super Admin changes a price.

Existing advertisements are backfilled from their current amount, duration,
start/end and creation timestamps. No existing amount is recalculated.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

PLAN_FK = "fk_advertisements_subscription_plan_id"
PLAN_INDEX = "ix_advertisements_subscription_plan_id"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS advertisement_subscription_plans (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                months INTEGER NOT NULL UNIQUE,
                label VARCHAR(50) NOT NULL,
                description TEXT,
                price NUMERIC(12, 2) NOT NULL DEFAULT 0,
                currency VARCHAR(10) NOT NULL DEFAULT 'INR',
                is_active BOOLEAN NOT NULL DEFAULT true,
                is_default BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_by UUID,
                updated_by UUID
            )
            """
        )
    )

    conn.execute(
        text(
            """
            INSERT INTO advertisement_subscription_plans
                (months, label, description, price, currency, is_active, is_default)
            VALUES
                (1, '1 Month', '1 Month advertisement subscription', 2000, 'INR', true, true),
                (2, '2 Months', '2 Months advertisement subscription', 4000, 'INR', true, false),
                (3, '3 Months', '3 Months advertisement subscription', 6000, 'INR', true, false)
            ON CONFLICT (months) DO NOTHING
            """
        )
    )

    conn.execute(
        text(
            """
            ALTER TABLE advertisements
                ADD COLUMN IF NOT EXISTS subscription_plan_id UUID,
                ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'INR',
                ADD COLUMN IF NOT EXISTS purchased_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS subscription_start_date TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS subscription_expiry_date TIMESTAMPTZ
            """
        )
    )

    fk_exists = conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": PLAN_FK},
    ).scalar_one_or_none()
    if fk_exists is None:
        conn.execute(
            text(
                f"ALTER TABLE advertisements ADD CONSTRAINT {PLAN_FK} "
                "FOREIGN KEY (subscription_plan_id) "
                "REFERENCES advertisement_subscription_plans(id) "
                "ON DELETE SET NULL"
            )
        )

    conn.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {PLAN_INDEX} "
            "ON advertisements (subscription_plan_id)"
        )
    )

    # Legacy purchases keep their stored amount/duration. Their dates are copied
    # into the new snapshot columns so historical records can be rendered exactly
    # as before without being re-priced.
    conn.execute(
        text(
            """
            UPDATE advertisements
               SET purchased_at = COALESCE(purchased_at, created_at),
                   subscription_start_date = COALESCE(
                       subscription_start_date, start_date, created_at
                   ),
                   subscription_expiry_date = COALESCE(
                       subscription_expiry_date, end_date
                   )
             WHERE purchased_at IS NULL
                OR subscription_start_date IS NULL
                OR subscription_expiry_date IS NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text(f"DROP INDEX IF EXISTS {PLAN_INDEX}"))
    conn.execute(
        text(f"ALTER TABLE advertisements DROP CONSTRAINT IF EXISTS {PLAN_FK}")
    )
    conn.execute(
        text(
            """
            ALTER TABLE advertisements
                DROP COLUMN IF EXISTS subscription_expiry_date,
                DROP COLUMN IF EXISTS subscription_start_date,
                DROP COLUMN IF EXISTS purchased_at,
                DROP COLUMN IF EXISTS currency,
                DROP COLUMN IF EXISTS subscription_plan_id
            """
        )
    )
    conn.execute(text("DROP TABLE IF EXISTS advertisement_subscription_plans"))
