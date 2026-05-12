"""
One-off verification script for the Phase 7.5 Polymarket Postgres stores.

Round-trips each of the 8 Postgres store classes against a live DB:
inserts known fixtures, asserts upsert/dedupe/sticky-manual/monotonic
semantics, then deletes the fixtures so the DB is fresh for real ingestion.

Usage:
    DATABASE_URL=postgresql://postgres:dev@localhost:5432/marketsignalos \
        .venv/Scripts/python.exe scripts/verify_polymarket_postgres.py
"""
from __future__ import annotations

import os
import sys

import psycopg2

from marketsignalos_polymarket.models import (
    MarketLink,
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketWalletEnrichment,
    PolymarketWalletValue,
)
from marketsignalos_polymarket.storage import (
    PostgresActivityStore,
    PostgresEnrichmentStore,
    PostgresLeaderboardStore,
    PostgresMarketLinkStore,
    PostgresMarketStore,
    PostgresPositionStore,
    PostgresWalletCheckpointStore,
    PostgresWalletValueStore,
)


def main() -> int:
    dsn = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:dev@localhost:5432/marketsignalos"
    )

    # 1. Leaderboard
    lb = PostgresLeaderboardStore(dsn)
    e1 = PolymarketLeaderboardEntry(
        proxy_wallet="0xabc", name="Theo4", pseudonym="Theo4",
        amount_usdc=22053933.75, metric="profit", window="all",
    )
    assert lb.write_leaderboard([e1]) == 1
    assert lb.write_leaderboard([e1]) == 1  # ON CONFLICT DO NOTHING
    print("OK leaderboard")

    # 2. Activity — verifies unix int -> TIMESTAMPTZ materialization
    act = PostgresActivityStore(dsn)
    a1 = PolymarketActivity(
        proxy_wallet="0xabc", timestamp=1715000000, condition_id="0xcond",
        type="TRADE", side="BUY", size=100.0, usdc_size=62.0, price=0.62,
        outcome_index=0, outcome="Yes", slug="m1", title="Market 1",
        event_slug="e1", transaction_hash="0xtx1",
    )
    a2 = PolymarketActivity(
        proxy_wallet="0xabc", timestamp=1715000000, condition_id="0xcond",
        type="TRADE", side="BUY", size=50.0, usdc_size=19.0, price=0.38,
        outcome_index=1, outcome="No", slug="m1", title="Market 1",
        event_slug="e1", transaction_hash="0xtx1",
    )
    assert act.write_activity([a1, a2]) == 2
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(
        "SELECT EXTRACT(EPOCH FROM traded_at)::bigint "
        "FROM polymarket_activity "
        "WHERE transaction_hash=%s AND outcome_index=0",
        ("0xtx1",),
    )
    row = cur.fetchone()
    assert row is not None
    ts = row[0]
    assert ts == 1715000000, f"traded_at expected 1715000000 got {ts}"
    print("OK activity (traded_at materialized correctly)")

    # 3. Positions
    pos = PostgresPositionStore(dsn)
    p1 = PolymarketPosition(
        proxy_wallet="0xabc", condition_id="0xcond", outcome_index=0, outcome="Yes",
        size=100, avg_price=0.62, current_value_usdc=68,
        slug="m1", title="t", event_slug="e1",
    )
    assert pos.write_positions([p1]) == 1
    print("OK positions")

    # 4. Markets — upsert + JSONB
    mkt = PostgresMarketStore(dsn)
    m1 = PolymarketMarket(
        gamma_id="1", condition_id="0xcond", slug="m1", question="q?",
        category="politics", end_date="2026-01-01T00:00:00Z",
        outcomes=["Yes", "No"], outcome_prices=[0.62, 0.38],
        volume_usdc=1000, liquidity_usdc=200, closed=False, active=True,
        last_trade_price=0.62, best_bid=0.61, best_ask=0.63,
    )
    assert mkt.write_markets([m1]) == 1
    m1_closed = PolymarketMarket(
        gamma_id="1", condition_id="0xcond", slug="m1", question="q?",
        category="politics", end_date="2026-01-01T00:00:00Z",
        outcomes=["Yes", "No"], outcome_prices=[1.0, 0.0],
        volume_usdc=1500, liquidity_usdc=0, closed=True, active=False,
        last_trade_price=1.0, best_bid=None, best_ask=None,
    )
    assert mkt.write_markets([m1_closed]) == 1
    cur.execute(
        "SELECT closed, active, outcomes::text FROM polymarket_markets "
        "WHERE condition_id=%s",
        ("0xcond",),
    )
    row = cur.fetchone()
    assert row is not None
    closed_val, active_val, outcomes_json = row
    assert closed_val is True and active_val is False, f"closed/active: {row}"
    assert outcomes_json == '["Yes", "No"]', f"outcomes JSONB: {outcomes_json!r}"
    print("OK markets (upsert + JSONB)")

    # 5. Wallet values
    wv = PostgresWalletValueStore(dsn)
    assert wv.write_values(
        [PolymarketWalletValue(proxy_wallet="0xabc", value_usdc=12345.67)]
    ) == 1
    print("OK wallet_values")

    # 6. Enrichment upsert
    enr = PostgresEnrichmentStore(dsn)
    e_first = PolymarketWalletEnrichment(
        proxy_wallet="0xabc", name="Theo4", pseudonym="Theo4",
        resolved_trades=50, wins=35, losses=15, win_rate=0.7,
        skill_likelihood=0.998, stddevs_above_expected=2.83,
        total_volume_usdc=500000, total_pnl_usdc=120000,
        avg_position_size_usdc=10000, trade_count=200,
        last_activity_at=1715000000,
    )
    assert enr.write_enrichment([e_first]) == 1
    e_second = PolymarketWalletEnrichment(
        proxy_wallet="0xabc", name="Theo4", pseudonym="Theo4",
        resolved_trades=60, wins=42, losses=18, win_rate=0.7,
        skill_likelihood=0.999, stddevs_above_expected=3.1,
        total_volume_usdc=600000, total_pnl_usdc=140000,
        avg_position_size_usdc=10000, trade_count=240,
        last_activity_at=1715100000,
    )
    assert enr.write_enrichment([e_second]) == 1
    cur.execute(
        "SELECT resolved_trades FROM polymarket_wallet_enrichment "
        "WHERE proxy_wallet=%s",
        ("0xabc",),
    )
    row = cur.fetchone()
    assert row is not None and row[0] == 60, "upsert should overwrite"
    print("OK enrichment (upsert overwrites)")

    # 7. Market links — sticky-manual
    ml = PostgresMarketLinkStore(dsn)
    auto_link = MarketLink(
        kalshi_ticker="KX1", polymarket_condition_id="0xp1", polymarket_slug="p1",
        kalshi_title="kt", polymarket_title="pt",
        kalshi_end_date="2026-01-01T00:00:00Z",
        polymarket_end_date="2026-01-01T00:00:00Z",
        confidence=0.7, status="pending", matched_by="auto",
    )
    assert ml.upsert_links([auto_link]) == 1
    manual_reject = MarketLink(
        kalshi_ticker="KX1", polymarket_condition_id="0xp1", polymarket_slug="p1",
        kalshi_title="kt", polymarket_title="pt",
        kalshi_end_date="2026-01-01T00:00:00Z",
        polymarket_end_date="2026-01-01T00:00:00Z",
        confidence=0.7, status="rejected", matched_by="manual",
    )
    assert ml.upsert_links([manual_reject]) == 1
    # Auto matcher re-runs — must NOT overwrite the manual rejection.
    assert ml.upsert_links([auto_link]) == 0, "sticky-manual should block re-upsert"
    loaded = ml.load_links()
    loaded = [link for link in loaded if link.kalshi_ticker == "KX1"]
    assert len(loaded) == 1
    assert loaded[0].status == "rejected" and loaded[0].matched_by == "manual", loaded[0]
    print("OK market_links (sticky-manual + load_links round-trip)")

    # 8. Wallet checkpoints — monotonic with case normalization
    ckpt = PostgresWalletCheckpointStore(dsn)
    assert ckpt.get_last_timestamp("0xABC") is None
    ckpt.set_last_timestamp("0xABC", 1700000000)
    assert ckpt.get_last_timestamp("0xabc") == 1700000000
    ckpt.set_last_timestamp("0xabc", 1600000000)
    assert ckpt.get_last_timestamp("0xabc") == 1700000000, \
        "older timestamp must not regress"
    ckpt.set_last_timestamp("0xabc", 1800000000)
    assert ckpt.get_last_timestamp("0xabc") == 1800000000
    print("OK wallet_checkpoints (monotonic + case normalization)")

    # Cleanup fixtures so the DB is clean for real ingestion.
    cur.execute(
        "DELETE FROM polymarket_leaderboard WHERE proxy_wallet=%s", ("0xabc",)
    )
    cur.execute(
        "DELETE FROM polymarket_activity WHERE transaction_hash=%s", ("0xtx1",)
    )
    cur.execute(
        "DELETE FROM polymarket_positions WHERE proxy_wallet=%s", ("0xabc",)
    )
    cur.execute(
        "DELETE FROM polymarket_markets WHERE condition_id=%s", ("0xcond",)
    )
    cur.execute(
        "DELETE FROM polymarket_wallet_values WHERE proxy_wallet=%s", ("0xabc",)
    )
    cur.execute(
        "DELETE FROM polymarket_wallet_enrichment WHERE proxy_wallet=%s", ("0xabc",)
    )
    cur.execute(
        "DELETE FROM polymarket_market_links WHERE kalshi_ticker=%s", ("KX1",)
    )
    cur.execute(
        "DELETE FROM polymarket_wallet_checkpoints WHERE proxy_wallet=%s", ("0xabc",)
    )
    conn.commit()
    conn.close()

    print()
    print("ALL 8 STORES VERIFIED END-TO-END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
