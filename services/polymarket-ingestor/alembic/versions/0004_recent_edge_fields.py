"""Add recency-weighted edge fields to wallet enrichment.

Revision ID: 0004_recent_edge_fields
Revises: 0003_wallet_trust_and_economics
Create Date: 2026-06-09
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_recent_edge_fields"
down_revision: str | None = "0003_wallet_trust_and_economics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS recent_skill_likelihood DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS recent_edge_mean DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS recent_edge_lower_bound DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS recent_independent_events DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS recent_independent_events,
            DROP COLUMN IF EXISTS recent_edge_lower_bound,
            DROP COLUMN IF EXISTS recent_edge_mean,
            DROP COLUMN IF EXISTS recent_skill_likelihood
        """
    )
