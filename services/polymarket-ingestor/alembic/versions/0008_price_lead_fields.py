"""Add price-lead (informed-flow footprint) fields to wallet enrichment.

Revision ID: 0008_price_lead_fields
Revises: 0007_category_edges
Create Date: 2026-07-07
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_price_lead_fields"
down_revision: str | None = "0007_category_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS price_lead_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS price_lead_drivers JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS post_entry_drift_48h DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS post_entry_drift_samples
                DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS longshot_win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS longshot_implied_rate
                DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS longshot_events DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS late_entry_win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS late_entry_implied_rate
                DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS late_entry_events DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS late_entry_events,
            DROP COLUMN IF EXISTS late_entry_implied_rate,
            DROP COLUMN IF EXISTS late_entry_win_rate,
            DROP COLUMN IF EXISTS longshot_events,
            DROP COLUMN IF EXISTS longshot_implied_rate,
            DROP COLUMN IF EXISTS longshot_win_rate,
            DROP COLUMN IF EXISTS post_entry_drift_samples,
            DROP COLUMN IF EXISTS post_entry_drift_48h,
            DROP COLUMN IF EXISTS price_lead_drivers,
            DROP COLUMN IF EXISTS price_lead_score
        """
    )
