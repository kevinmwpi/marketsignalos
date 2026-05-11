from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from marketsignalos_polymarket.models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketWalletValue,
)
from marketsignalos_polymarket.storage import (
    JsonlActivityStore,
    JsonlLeaderboardStore,
    JsonlMarketStore,
    JsonlPositionStore,
    JsonlWalletValueStore,
)


def test_leaderboard_store_appends(tmp_path: Path) -> None:
    store = JsonlLeaderboardStore(tmp_path / "lb.jsonl")
    entry = PolymarketLeaderboardEntry(
        proxy_wallet="0xabc",
        name="Theo4",
        pseudonym="Theo4",
        amount_usdc=22_053_933.75,
        metric="profit",
        window="all",
    )
    assert store.write_leaderboard([entry]) == 1
    line = (tmp_path / "lb.jsonl").read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["proxy_wallet"] == "0xabc"
    assert payload["amount_usdc"] == 22_053_933.75


def test_activity_store_dedupes_on_tx_hash_condition_outcome(tmp_path: Path) -> None:
    store = JsonlActivityStore(tmp_path / "activity.jsonl")
    a = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=1715000000,
        condition_id="0xcond",
        type="TRADE",
        side="BUY",
        size=100.0,
        usdc_size=62.0,
        price=0.62,
        outcome_index=0,
        outcome="Yes",
        slug="some-market",
        title="Some market",
        event_slug="some-event",
        transaction_hash="0xtx1",
    )
    # same tx_hash but different outcome leg should NOT dedupe
    b = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=1715000000,
        condition_id="0xcond",
        type="TRADE",
        side="BUY",
        size=50.0,
        usdc_size=19.0,
        price=0.38,
        outcome_index=1,
        outcome="No",
        slug="some-market",
        title="Some market",
        event_slug="some-event",
        transaction_hash="0xtx1",
    )
    # exact duplicate of `a` should dedupe
    a_dup = replace(a)

    assert store.write_activity([a, b]) == 2
    assert store.write_activity([a_dup]) == 0

    lines = (tmp_path / "activity.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_market_store_dedupes_on_condition_plus_closed_status(tmp_path: Path) -> None:
    store = JsonlMarketStore(tmp_path / "markets.jsonl")
    open_market = PolymarketMarket(
        gamma_id="1", condition_id="0xc", slug="s", question="q?", category="cat",
        end_date="2026-01-01T00:00:00Z", outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
        volume_usdc=1000, liquidity_usdc=200, closed=False, active=True,
        last_trade_price=0.5, best_bid=0.49, best_ask=0.51,
    )
    closed_market = replace(open_market, closed=True, active=False)

    assert store.write_markets([open_market]) == 1
    # second call with same open record dedupes
    assert store.write_markets([open_market]) == 0
    # but the transition open->closed produces a new row
    assert store.write_markets([closed_market]) == 1


def test_position_store_appends_snapshots(tmp_path: Path) -> None:
    store = JsonlPositionStore(tmp_path / "positions.jsonl")
    pos = PolymarketPosition(
        proxy_wallet="0xabc", condition_id="0xc", outcome_index=0, outcome="Yes",
        size=100, avg_price=0.62, current_value_usdc=68, slug="s", title="t", event_slug="e",
    )
    assert store.write_positions([pos]) == 1
    # Snapshots: writing the same position twice appends, doesn't dedupe.
    assert store.write_positions([pos]) == 1
    lines = (tmp_path / "positions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_wallet_value_store(tmp_path: Path) -> None:
    store = JsonlWalletValueStore(tmp_path / "values.jsonl")
    assert store.write_values([PolymarketWalletValue(proxy_wallet="0xabc", value_usdc=12345.67)]) == 1
