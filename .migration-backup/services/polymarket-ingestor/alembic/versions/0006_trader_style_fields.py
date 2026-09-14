"""Add trader-style (archetype) fields to wallet enrichment.

Revision ID: 0006_trader_style_fields
Revises: 0005_clv_wallet_bets_price_history
Create Date: 2026-06-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_trader_style_fields"
down_revision: str | None = "0005_clv_wallet_bets_price_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS style_archetype TEXT NOT NULL DEFAULT 'unclassified',
            ADD COLUMN IF NOT EXISTS automation_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS style_drivers JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS trades_per_active_day DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS median_intertrade_gap_seconds
                DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS active_utc_hours INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS buy_size_uniformity DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS markets_per_active_day DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS top_category TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS top_category_share DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS exited_position_share DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS median_exit_hold_hours DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS median_exit_hold_hours,
            DROP COLUMN IF EXISTS exited_position_share,
            DROP COLUMN IF EXISTS top_category_share,
            DROP COLUMN IF EXISTS top_category,
            DROP COLUMN IF EXISTS markets_per_active_day,
            DROP COLUMN IF EXISTS buy_size_uniformity,
            DROP COLUMN IF EXISTS active_utc_hours,
            DROP COLUMN IF EXISTS median_intertrade_gap_seconds,
            DROP COLUMN IF EXISTS trades_per_active_day,
            DROP COLUMN IF EXISTS style_drivers,
            DROP COLUMN IF EXISTS automation_score,
            DROP COLUMN IF EXISTS style_archetype
        """
    )
