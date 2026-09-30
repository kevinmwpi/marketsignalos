"""Add Bayesian-edge fields to polymarket_wallet_enrichment.

skill_likelihood now means P(edge > 0 | data); we add edge_mean,
edge_lower_bound, effective_sample_size, resolved_volume_usdc and
rank_score so downstream consumers can rank conservatively against
the market's own implied odds.

Revision ID: 0002_bayesian_edge_fields
Revises: 0001_polymarket_initial
Create Date: 2026-05-17
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_bayesian_edge_fields"
down_revision: str | None = "0001_polymarket_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Five new columns: defaulting to 0 keeps existing rows readable
    # while the next enrichment pass overwrites them.
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS edge_mean             DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS edge_lower_bound      DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS effective_sample_size DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS resolved_volume_usdc  DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS rank_score            DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )
    # rank_score is the canonical sort key for the leaderboard view.
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_wallet_enrichment_rank "
        "ON polymarket_wallet_enrichment (rank_score DESC)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS polymarket_wallet_enrichment_rank"
    )
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS rank_score,
            DROP COLUMN IF EXISTS resolved_volume_usdc,
            DROP COLUMN IF EXISTS effective_sample_size,
            DROP COLUMN IF EXISTS edge_lower_bound,
            DROP COLUMN IF EXISTS edge_mean
        """
    )
