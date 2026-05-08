"""Initial schema: trades, fills, market_resolutions, account_enrichment, checkpoints.

Revision ID: 0001
Revises:
Create Date: 2026-05-08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── trades ────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            source          TEXT        NOT NULL,
            market_ticker   TEXT        NOT NULL,
            trade_id        TEXT        NOT NULL,
            side            TEXT        NOT NULL CHECK (side IN ('yes', 'no')),
            price           NUMERIC(10, 4) NOT NULL,
            quantity        INTEGER     NOT NULL CHECK (quantity > 0),
            traded_at       TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (source, trade_id)
        )
        """
    )

    # Attempt to create a TimescaleDB hypertable for efficient time-range queries.
    # Silently skipped when the TimescaleDB extension is not installed.
    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('trades', 'traded_at', if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )

    # ── fills ─────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            source          TEXT        NOT NULL,
            account_id      TEXT        NOT NULL,
            market_ticker   TEXT        NOT NULL,
            fill_id         TEXT        NOT NULL,
            trade_id        TEXT        NOT NULL,
            side            TEXT        NOT NULL CHECK (side IN ('yes', 'no')),
            price           NUMERIC(10, 4) NOT NULL,
            quantity        INTEGER     NOT NULL CHECK (quantity > 0),
            traded_at       TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (source, fill_id)
        )
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('fills', 'traded_at', if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )

    # Speeds up per-account leaderboard queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS fills_account_traded ON fills (account_id, traded_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS fills_market_traded ON fills (market_ticker, traded_at DESC)"
    )

    # ── market_resolutions ────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS market_resolutions (
            source          TEXT        NOT NULL,
            market_ticker   TEXT        NOT NULL,
            resolved_at     TIMESTAMPTZ NOT NULL,
            settlement_side TEXT        NOT NULL CHECK (settlement_side IN ('yes', 'no')),
            PRIMARY KEY (source, market_ticker)
        )
        """
    )

    # ── account_enrichment ────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS account_enrichment (
            account_id                  TEXT            PRIMARY KEY,
            cross_market_coordination   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            pre_resolution_accuracy     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            fill_intensity              DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at                  TIMESTAMPTZ     NOT NULL DEFAULT now()
        )
        """
    )

    # ── ingestor_checkpoints ──────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestor_checkpoints (
            key     TEXT PRIMARY KEY,
            cursor  TEXT
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingestor_checkpoints")
    op.execute("DROP TABLE IF EXISTS account_enrichment")
    op.execute("DROP TABLE IF EXISTS market_resolutions")
    op.execute("DROP TABLE IF EXISTS fills")
    op.execute("DROP TABLE IF EXISTS trades")
