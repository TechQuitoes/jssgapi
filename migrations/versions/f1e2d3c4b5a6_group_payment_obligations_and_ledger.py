"""add group payment obligations ledger and webhook events

Revision ID: f1e2d3c4b5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-11-14 00:00:00.000000

Adds group payment obligations table, webhook events tracking table for idempotency,
and extends payments and payment_transactions with group/obligation foreign keys,
payment link attributes, and financial check/unique constraints.

All financial relationships use ON DELETE RESTRICT / SET NULL so cascading group
deletion can never destroy financial history.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "f1e2d3c4b5a6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create group_payment_obligations table with check constraints and unique constraint
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS group_payment_obligations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                group_id UUID REFERENCES social_groups(id) ON DELETE RESTRICT,
                group_name VARCHAR(255) NOT NULL,
                billing_period VARCHAR(20) NOT NULL,
                fee_id UUID REFERENCES fees(id) ON DELETE SET NULL,
                member_count_snapshot INTEGER NOT NULL DEFAULT 0,
                rate_per_member NUMERIC(12, 2) NOT NULL DEFAULT 100.00,
                expected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                collected_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                pending_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                status VARCHAR(30) NOT NULL DEFAULT 'UNPAID',
                active_payment_link_id VARCHAR(100),
                active_payment_link_url TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_by UUID,
                updated_by UUID,
                CONSTRAINT uq_group_obligation_period UNIQUE (group_id, billing_period),
                CONSTRAINT ck_obligation_member_count CHECK (member_count_snapshot >= 0),
                CONSTRAINT ck_obligation_rate_per_member CHECK (rate_per_member >= 0),
                CONSTRAINT ck_obligation_expected_amount CHECK (expected_amount >= 0),
                CONSTRAINT ck_obligation_collected_amount CHECK (collected_amount >= 0),
                CONSTRAINT ck_obligation_pending_amount CHECK (pending_amount >= 0)
            );
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_group_payment_obligations_group_id
                ON group_payment_obligations (group_id);
            CREATE INDEX IF NOT EXISTS ix_group_payment_obligations_status
                ON group_payment_obligations (status);
            CREATE INDEX IF NOT EXISTS ix_group_payment_obligations_period
                ON group_payment_obligations (billing_period);
            """
        )
    )

    # 2. Create webhook_events table for strict idempotency
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                event_id VARCHAR(100) NOT NULL UNIQUE,
                event_type VARCHAR(60) NOT NULL,
                payment_id VARCHAR(100),
                order_id VARCHAR(100),
                payment_link_id VARCHAR(100),
                status VARCHAR(30) NOT NULL DEFAULT 'PROCESSED',
                raw_payload TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_webhook_events_event_id
                ON webhook_events (event_id);
            CREATE INDEX IF NOT EXISTS ix_webhook_events_payment_id
                ON webhook_events (payment_id);
            CREATE INDEX IF NOT EXISTS ix_webhook_events_order_id
                ON webhook_events (order_id);
            """
        )
    )

    # 3. Enhance payments table
    conn.execute(
        text(
            """
            ALTER TABLE payments
                ADD COLUMN IF NOT EXISTS group_id UUID REFERENCES social_groups(id) ON DELETE SET NULL,
                ADD COLUMN IF NOT EXISTS obligation_id UUID REFERENCES group_payment_obligations(id) ON DELETE RESTRICT,
                ADD COLUMN IF NOT EXISTS payment_link_id VARCHAR(100),
                ADD COLUMN IF NOT EXISTS workflow VARCHAR(50);
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_payments_group_id
                ON payments (group_id);
            CREATE INDEX IF NOT EXISTS ix_payments_obligation_id
                ON payments (obligation_id);
            CREATE INDEX IF NOT EXISTS ix_payments_payment_link_id
                ON payments (payment_link_id);
            """
        )
    )

    # 4. Enhance payment_transactions table
    conn.execute(
        text(
            """
            ALTER TABLE payment_transactions
                ADD COLUMN IF NOT EXISTS group_id UUID REFERENCES social_groups(id) ON DELETE SET NULL,
                ADD COLUMN IF NOT EXISTS obligation_id UUID REFERENCES group_payment_obligations(id) ON DELETE RESTRICT,
                ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50),
                ADD COLUMN IF NOT EXISTS razorpay_order_id VARCHAR(100),
                ADD COLUMN IF NOT EXISTS razorpay_payment_id VARCHAR(100),
                ADD COLUMN IF NOT EXISTS razorpay_payment_link_id VARCHAR(100),
                ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_payment_transactions_group_id
                ON payment_transactions (group_id);
            CREATE INDEX IF NOT EXISTS ix_payment_transactions_obligation_id
                ON payment_transactions (obligation_id);
            CREATE INDEX IF NOT EXISTS ix_payment_transactions_provider_txn_id
                ON payment_transactions (provider_transaction_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_transactions_rzp_payment_id
                ON payment_transactions (razorpay_payment_id)
                WHERE razorpay_payment_id IS NOT NULL;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # 1. Revert payment_transactions additions
    conn.execute(
        text(
            """
            DROP INDEX IF EXISTS uq_payment_transactions_rzp_payment_id;
            DROP INDEX IF EXISTS ix_payment_transactions_provider_txn_id;
            DROP INDEX IF EXISTS ix_payment_transactions_obligation_id;
            DROP INDEX IF EXISTS ix_payment_transactions_group_id;
            ALTER TABLE payment_transactions
                DROP COLUMN IF EXISTS paid_at,
                DROP COLUMN IF EXISTS razorpay_payment_link_id,
                DROP COLUMN IF EXISTS razorpay_payment_id,
                DROP COLUMN IF EXISTS razorpay_order_id,
                DROP COLUMN IF EXISTS payment_method,
                DROP COLUMN IF EXISTS obligation_id,
                DROP COLUMN IF EXISTS group_id;
            """
        )
    )

    # 2. Revert payments additions
    conn.execute(
        text(
            """
            DROP INDEX IF EXISTS ix_payments_payment_link_id;
            DROP INDEX IF EXISTS ix_payments_obligation_id;
            DROP INDEX IF EXISTS ix_payments_group_id;
            ALTER TABLE payments
                DROP COLUMN IF EXISTS workflow,
                DROP COLUMN IF EXISTS payment_link_id,
                DROP COLUMN IF EXISTS obligation_id,
                DROP COLUMN IF EXISTS group_id;
            """
        )
    )

    # 3. Drop webhook_events table
    conn.execute(text("DROP TABLE IF EXISTS webhook_events CASCADE;"))

    # 4. Drop group_payment_obligations table
    conn.execute(text("DROP TABLE IF EXISTS group_payment_obligations CASCADE;"))
