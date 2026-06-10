"""Add recency-weighted edge fields to wallet enrichment.

Revision ID: 0004_recent_edge_fields
Revises: 0003_wallet_trust_and_economics
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_recent_edge_fields"
down_revision: Union[str, None] = "0003_wallet_trust_and_economics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
