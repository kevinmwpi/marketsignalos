"""Initial Polymarket schema: leaderboard, activity, positions, markets,
wallet_values, wallet_enrichment, market_links, wallet_checkpoints.

Revision ID: 0001_polymarket_initial
Revises:
Create Date: 2026-05-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_polymarket_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── polymarket_leaderboard ────────────────────────────────────────────────
    # Snapshot rows; every pull is preserved so historical rank movement stays
    # queryable. PK includes fetched_at to allow repeated snapshots per wallet.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_leaderboard (
            proxy_wallet    TEXT             NOT NULL,
            name            TEXT             NOT NULL DEFAULT '',
            pseudonym       TEXT             NOT NULL DEFAULT '',
            amount_usdc     DOUBLE PRECISION NOT NULL,
            metric          TEXT             NOT NULL,
            -- `window` is a reserved word in Postgres (window functions),
            -- so the column is named window_label and mapped from the
            -- dataclass field `.window` at the store layer.
            window_label    TEXT             NOT NULL,
            fetched_at      TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (proxy_wallet, metric, window_label, fetched_at)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('polymarket_leaderboard', 'fetched_at',
                                      if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )

    # ── polymarket_activity ───────────────────────────────────────────────────
    # On-chain trade/redeem events. One transaction can settle multiple legs
    # (different condition_id / outcome_index), hence the composite PK.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_activity (
            proxy_wallet        TEXT             NOT NULL,
            transaction_hash    TEXT             NOT NULL,
            condition_id        TEXT             NOT NULL,
            outcome_index       INTEGER          NOT NULL,
            type                TEXT             NOT NULL,
            side                TEXT             NOT NULL DEFAULT '',
            size                DOUBLE PRECISION NOT NULL,
            usdc_size           DOUBLE PRECISION NOT NULL,
            price               DOUBLE PRECISION NOT NULL,
            outcome             TEXT             NOT NULL DEFAULT '',
            slug                TEXT             NOT NULL DEFAULT '',
            title               TEXT             NOT NULL DEFAULT '',
            event_slug          TEXT             NOT NULL DEFAULT '',
            name                TEXT             NOT NULL DEFAULT '',
            pseudonym           TEXT             NOT NULL DEFAULT '',
            traded_at           TIMESTAMPTZ      NOT NULL,
            fetched_at          TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (transaction_hash, condition_id, outcome_index, type)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('polymarket_activity', 'traded_at',
                                      if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )
    # Per-wallet activity scan is the hottest access pattern (skill computation,
    # checkpoint advance) — index supports descending traded_at.
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_activity_wallet_traded "
        "ON polymarket_activity (proxy_wallet, traded_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_activity_condition "
        "ON polymarket_activity (condition_id)"
    )

    # ── polymarket_positions ──────────────────────────────────────────────────
    # Snapshot semantics — each fetch is a fresh row keyed on snapshot_at so
    # downstream readers can pick the most recent. Hypertable for cheap pruning.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_positions (
            proxy_wallet         TEXT             NOT NULL,
            condition_id         TEXT             NOT NULL,
            outcome_index        INTEGER          NOT NULL,
            outcome              TEXT             NOT NULL DEFAULT '',
            size                 DOUBLE PRECISION NOT NULL,
            avg_price            DOUBLE PRECISION NOT NULL,
            current_value_usdc   DOUBLE PRECISION NOT NULL,
            slug                 TEXT             NOT NULL DEFAULT '',
            title                TEXT             NOT NULL DEFAULT '',
            event_slug           TEXT             NOT NULL DEFAULT '',
            snapshot_at          TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (proxy_wallet, condition_id, outcome_index, snapshot_at)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('polymarket_positions', 'snapshot_at',
                                      if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_positions_wallet_snapshot "
        "ON polymarket_positions (proxy_wallet, snapshot_at DESC)"
    )

    # ── polymarket_markets ────────────────────────────────────────────────────
    # Reference data — one row per condition_id, always overwritten to latest.
    # Not a hypertable because we don't keep history (Gamma is the source of truth).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_markets (
            condition_id        TEXT             PRIMARY KEY,
            gamma_id            TEXT             NOT NULL,
            slug                TEXT             NOT NULL,
            question            TEXT             NOT NULL,
            category            TEXT             NOT NULL DEFAULT '',
            end_date            TEXT             NOT NULL,
            outcomes            JSONB            NOT NULL DEFAULT '[]'::jsonb,
            outcome_prices      JSONB            NOT NULL DEFAULT '[]'::jsonb,
            volume_usdc         DOUBLE PRECISION NOT NULL DEFAULT 0,
            liquidity_usdc      DOUBLE PRECISION NOT NULL DEFAULT 0,
            closed              BOOLEAN          NOT NULL DEFAULT FALSE,
            active              BOOLEAN          NOT NULL DEFAULT TRUE,
            last_trade_price    DOUBLE PRECISION,
            best_bid            DOUBLE PRECISION,
            best_ask            DOUBLE PRECISION,
            fetched_at          TIMESTAMPTZ      NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_markets_end_date "
        "ON polymarket_markets (end_date)"
    )

    # ── polymarket_wallet_values ──────────────────────────────────────────────
    # Per-wallet portfolio-value snapshots.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_wallet_values (
            proxy_wallet    TEXT             NOT NULL,
            value_usdc      DOUBLE PRECISION NOT NULL,
            snapshot_at     TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (proxy_wallet, snapshot_at)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            PERFORM create_hypertable('polymarket_wallet_values', 'snapshot_at',
                                      if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        $$
        """
    )

    # ── polymarket_wallet_enrichment ──────────────────────────────────────────
    # One row per wallet — recomputed each enrichment pass.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_wallet_enrichment (
            proxy_wallet              TEXT             PRIMARY KEY,
            name                      TEXT             NOT NULL DEFAULT '',
            pseudonym                 TEXT             NOT NULL DEFAULT '',
            resolved_trades           INTEGER          NOT NULL,
            wins                      INTEGER          NOT NULL,
            losses                    INTEGER          NOT NULL,
            win_rate                  DOUBLE PRECISION NOT NULL,
            skill_likelihood          DOUBLE PRECISION NOT NULL,
            stddevs_above_expected    DOUBLE PRECISION NOT NULL,
            total_volume_usdc         DOUBLE PRECISION NOT NULL,
            total_pnl_usdc            DOUBLE PRECISION NOT NULL,
            avg_position_size_usdc    DOUBLE PRECISION NOT NULL,
            trade_count               INTEGER          NOT NULL,
            last_activity_at          BIGINT           NOT NULL,
            computed_at               TIMESTAMPTZ      NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_wallet_enrichment_skill "
        "ON polymarket_wallet_enrichment (skill_likelihood DESC)"
    )

    # ── polymarket_market_links ───────────────────────────────────────────────
    # Polymarket → Kalshi market mapping. Sticky-manual semantics are enforced by the
    # Python store at upsert time (ON CONFLICT ... WHERE matched_by <> 'manual').
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_market_links (
            kalshi_ticker                TEXT             NOT NULL,
            polymarket_condition_id      TEXT             NOT NULL,
            polymarket_slug              TEXT             NOT NULL DEFAULT '',
            kalshi_title                 TEXT             NOT NULL DEFAULT '',
            polymarket_title             TEXT             NOT NULL DEFAULT '',
            kalshi_end_date              TEXT             NOT NULL DEFAULT '',
            polymarket_end_date          TEXT             NOT NULL DEFAULT '',
            confidence                   DOUBLE PRECISION NOT NULL,
            status                       TEXT             NOT NULL,
            matched_by                   TEXT             NOT NULL,
            matched_at                   TIMESTAMPTZ      NOT NULL,
            PRIMARY KEY (kalshi_ticker, polymarket_condition_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS polymarket_market_links_status "
        "ON polymarket_market_links (status)"
    )

    # ── polymarket_wallet_checkpoints ─────────────────────────────────────────
    # Per-wallet last-seen activity timestamp (unix seconds) — monotonic.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_wallet_checkpoints (
            proxy_wallet    TEXT   PRIMARY KEY,
            last_timestamp  BIGINT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS polymarket_wallet_checkpoints")
    op.execute("DROP TABLE IF EXISTS polymarket_market_links")
    op.execute("DROP TABLE IF EXISTS polymarket_wallet_enrichment")
    op.execute("DROP TABLE IF EXISTS polymarket_wallet_values")
    op.execute("DROP TABLE IF EXISTS polymarket_markets")
    op.execute("DROP TABLE IF EXISTS polymarket_positions")
    op.execute("DROP TABLE IF EXISTS polymarket_activity")
    op.execute("DROP TABLE IF EXISTS polymarket_leaderboard")
