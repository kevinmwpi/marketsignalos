"""Add per-category edge decomposition to wallet enrichment.

Revision ID: 0007_category_edges
Revises: 0006_trader_style_fields
Create Date: 2026-07-06
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_category_edges"
down_revision: str | None = "0006_trader_style_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            ADD COLUMN IF NOT EXISTS category_edges JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE polymarket_wallet_enrichment
            DROP COLUMN IF EXISTS category_edges
        """
    )
