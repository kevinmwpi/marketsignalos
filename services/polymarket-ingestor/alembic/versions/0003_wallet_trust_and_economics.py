"""Add wallet trust, economics, and complete snapshot state.

Revision ID: 0003_wallet_trust_and_economics
Revises: 0002_bayesian_edge_fields
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_wallet_trust_and_economics"
down_revision: Union[str, None] = "0002_bayesian_edge_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_positions
            ADD COLUMN IF NOT EXISTS current_outcome_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS cash_pnl_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS snapshot_id TEXT NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_position_snapshots (
            proxy_wallet    TEXT        PRIMARY KEY,
            snapshot_id     TEXT        NOT NULL,
            position_count  INTEGER     NOT NULL,
            complete        BOOLEAN     NOT NULL,
            snapshot_at     TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_closed_positions (
            proxy_wallet          TEXT             NOT NULL,
            condition_id          TEXT             NOT NULL,
            outcome_index         INTEGER          NOT NULL,
            outcome               TEXT             NOT NULL DEFAULT '',
            size                  DOUBLE PRECISION NOT NULL,
            avg_price             DOUBLE PRECISION NOT NULL,
            realized_pnl_usdc     DOUBLE PRECISION NOT NULL,
            current_outcome_price DOUBLE PRECISION NOT NULL,
            slug                  TEXT             NOT NULL DEFAULT '',
            title                 TEXT             NOT NULL DEFAULT '',
            event_slug            TEXT             NOT NULL DEFAULT '',
            fetched_at            TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (proxy_wallet, condition_id, outcome_index)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_wallet_hydration (
            proxy_wallet                     TEXT PRIMARY KEY,
            newest_activity_timestamp        BIGINT,
            oldest_activity_cursor_timestamp BIGINT,
            activity_history_complete        BOOLEAN NOT NULL DEFAULT FALSE,
            positions_complete               BOOLEAN NOT NULL DEFAULT FALSE,
            closed_positions_complete        BOOLEAN NOT NULL DEFAULT FALSE,
            economic_all_time_complete       BOOLEAN NOT NULL DEFAULT FALSE,
            economic_month_complete          BOOLEAN NOT NULL DEFAULT FALSE,
            metadata_condition_count         INTEGER NOT NULL DEFAULT 0,
            metadata_covered_count           INTEGER NOT NULL DEFAULT 0,
            metadata_coverage                DOUBLE PRECISION NOT NULL DEFAULT 0,
            all_time_pnl_usdc                DOUBLE PRECISION NOT NULL DEFAULT 0,
            all_time_volume_usdc             DOUBLE PRECISION NOT NULL DEFAULT 0,
            pnl_30d_usdc                     DOUBLE PRECISION NOT NULL DEFAULT 0,
            active_pnl_usdc                  DOUBLE PRECISION NOT NULL DEFAULT 0,
            max_drawdown_usdc                DOUBLE PRECISION NOT NULL DEFAULT 0,
            latest_position_snapshot_id      TEXT,
            last_refreshed_at                TIMESTAMPTZ,
            errors                           JSONB NOT NULL DEFAULT '[]'::jsonb
        )
        """
    )
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS forecast_skill_likelihood DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS forecast_edge_mean DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS forecast_edge_lower_bound DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS independent_settled_events DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS all_time_pnl_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS all_time_volume_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS all_time_roi DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS pnl_30d_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS active_pnl_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS max_drawdown_usdc DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS data_quality_status TEXT NOT NULL DEFAULT 'untrusted',
            ADD COLUMN IF NOT EXISTS data_quality_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS economic_qualified BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS tailability_status TEXT NOT NULL DEFAULT 'blocked',
            ADD COLUMN IF NOT EXISTS tailability_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS score_version TEXT NOT NULL DEFAULT 'legacy'
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_wallet_enrichment_tailability "
        "ON polymarket_wallet_enrichment (tailability_status, rank_score DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS polymarket_wallet_enrichment_tailability")
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS score_version,
            DROP COLUMN IF EXISTS tailability_reasons,
            DROP COLUMN IF EXISTS tailability_status,
            DROP COLUMN IF EXISTS economic_qualified,
            DROP COLUMN IF EXISTS data_quality_reasons,
            DROP COLUMN IF EXISTS data_quality_status,
            DROP COLUMN IF EXISTS max_drawdown_usdc,
            DROP COLUMN IF EXISTS active_pnl_usdc,
            DROP COLUMN IF EXISTS pnl_30d_usdc,
            DROP COLUMN IF EXISTS all_time_roi,
            DROP COLUMN IF EXISTS all_time_volume_usdc,
            DROP COLUMN IF EXISTS all_time_pnl_usdc,
            DROP COLUMN IF EXISTS independent_settled_events,
            DROP COLUMN IF EXISTS forecast_edge_lower_bound,
            DROP COLUMN IF EXISTS forecast_edge_mean,
            DROP COLUMN IF EXISTS forecast_skill_likelihood
        """
    )
    op.execute("DROP TABLE IF EXISTS polymarket_wallet_hydration")
    op.execute("DROP TABLE IF EXISTS polymarket_closed_positions")
    op.execute("DROP TABLE IF EXISTS polymarket_position_snapshots")
    op.execute(
        """
        ALTER TABLE polymarket_positions
            DROP COLUMN IF EXISTS snapshot_id,
            DROP COLUMN IF EXISTS cash_pnl_usdc,
            DROP COLUMN IF EXISTS current_outcome_price
        """
    )
