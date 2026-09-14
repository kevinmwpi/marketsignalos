"""Add CLV enrichment fields, wallet bet records, and price history.

Revision ID: 0005_clv_wallet_bets_price_history
Revises: 0004_recent_edge_fields
Create Date: 2026-06-10
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_clv_wallet_bets_price_history"
down_revision: str | None = "0004_recent_edge_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS clv_mean DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS clv_lower_bound DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS clv_sample_size DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_wallet_bets (
            proxy_wallet      TEXT             NOT NULL,
            condition_id      TEXT             NOT NULL,
            outcome_index     INTEGER          NOT NULL,
            outcome           TEXT             NOT NULL DEFAULT '',
            event_slug        TEXT             NOT NULL DEFAULT '',
            category          TEXT             NOT NULL DEFAULT '',
            title             TEXT             NOT NULL DEFAULT '',
            status            TEXT             NOT NULL,
            entry_price       DOUBLE PRECISION NOT NULL,
            cost_usdc         DOUBLE PRECISION NOT NULL,
            net_size          DOUBLE PRECISION NOT NULL,
            realized_pnl_usdc DOUBLE PRECISION NOT NULL,
            total_pnl_usdc    DOUBLE PRECISION NOT NULL,
            clv               DOUBLE PRECISION,
            last_trade_ts     BIGINT           NOT NULL DEFAULT 0,
            computed_at       TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (proxy_wallet, condition_id, outcome_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_wallet_bets_wallet_ts "
        "ON polymarket_wallet_bets (proxy_wallet, last_trade_ts DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_price_snapshots (
            condition_id TEXT             NOT NULL,
            yes_price    DOUBLE PRECISION NOT NULL,
            active       BOOLEAN          NOT NULL,
            closed       BOOLEAN          NOT NULL,
            fetched_at   TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (condition_id, fetched_at)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS polymarket_price_snapshots")
    op.execute("DROP INDEX IF EXISTS polymarket_wallet_bets_wallet_ts")
    op.execute("DROP TABLE IF EXISTS polymarket_wallet_bets")
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS clv_sample_size,
            DROP COLUMN IF EXISTS clv_lower_bound,
            DROP COLUMN IF EXISTS clv_mean
        """
    )
