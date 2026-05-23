from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def _seed(tmp_path: Path) -> Path:
    """
    Fixture covers four scenarios in one DB so each test only needs to
    assert what it's checking:
      - alpha:    skilled, holds an OPEN position, has a recent BUY  → INCLUDED
      - alpha:    a SECOND held position with an older BUY           → INCLUDED (older)
      - alpha:    a position that has been fully sold (size=0)        → EXCLUDED
      - noob:     not skilled — also has held position + buy         → EXCLUDED
      - ghost:    skilled and has a snapshot but no matching BUY      → EXCLUDED
    """
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)

    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "name": "AlphaWhale", "pseudonym": "AlphaWhale",
                "resolved_trades": 50, "wins": 38, "losses": 12,
                "win_rate": 0.76, "skill_likelihood": 0.99, "stddevs_above_expected": 3.7,
                "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
                "avg_position_size_usdc": 20_000, "trade_count": 200,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
            # noob — should fail min_skill.
            {
                "proxy_wallet": "0xnoob", "name": "Newbie", "pseudonym": "Newbie",
                "resolved_trades": 50, "wins": 20, "losses": 30,
                "win_rate": 0.4, "skill_likelihood": 0.05, "stddevs_above_expected": -2.8,
                "total_volume_usdc": 1000, "total_pnl_usdc": -500,
                "avg_position_size_usdc": 30, "trade_count": 60,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
            # ghost — skilled but no matching BUY event.
            {
                "proxy_wallet": "0xghost", "name": "GhostHolder", "pseudonym": "GhostHolder",
                "resolved_trades": 40, "wins": 30, "losses": 10,
                "win_rate": 0.75, "skill_likelihood": 0.95, "stddevs_above_expected": 3.0,
                "total_volume_usdc": 500_000, "total_pnl_usdc": 100_000,
                "avg_position_size_usdc": 10_000, "trade_count": 100,
                "last_activity_at": 1731000000, "computed_at": "2026-05-12T00:00:00Z",
            },
        ],
    )

    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            # alpha recent buy — still held, biggest current size.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_fed",
                "outcome_index": 0, "outcome": "Yes",
                "size": 5000.0, "avg_price": 0.45, "current_value_usdc": 2900.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # alpha older buy — still held but bought longer ago.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_btc",
                "outcome_index": 0, "outcome": "Yes",
                "size": 2000.0, "avg_price": 0.30, "current_value_usdc": 800.0,
                "slug": "btc-100k-eoy", "title": "Bitcoin closes 2026 above $100k",
                "event_slug": "btc-eoy-2026",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # alpha fully exited — must NOT appear in the feed.
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_exit",
                "outcome_index": 0, "outcome": "Yes",
                "size": 0.0, "avg_price": 0.55, "current_value_usdc": 0.0,
                "slug": "exited", "title": "Already exited",
                "event_slug": "exited-event",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # noob's held position — must NOT appear (low skill).
            {
                "proxy_wallet": "0xnoob", "condition_id": "0xcond_fed",
                "outcome_index": 0, "outcome": "Yes",
                "size": 10.0, "avg_price": 0.45, "current_value_usdc": 5.8,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
            # ghost holds a position but we have NO matching buy event for it.
            {
                "proxy_wallet": "0xghost", "condition_id": "0xcond_btc",
                "outcome_index": 0, "outcome": "Yes",
                "size": 100.0, "avg_price": 0.32, "current_value_usdc": 40.0,
                "slug": "btc-100k-eoy", "title": "Bitcoin closes 2026 above $100k",
                "event_slug": "btc-eoy-2026",
                "snapshot_at": "2026-05-12T08:00:00Z",
            },
        ],
    )

    _write_jsonl(
        d / "polymarket_activity.jsonl",
        [
            # alpha bought fed-50bp twice; latest should be the entry shown.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731000000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 3000.0, "usdc_size": 1350.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_old",
            },
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731500000,  # NEWER
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 900.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_new",
            },
            # alpha BTC buy — older entry, still held.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1730000000,
                "condition_id": "0xcond_btc", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 600.0, "price": 0.30,
                "outcome_index": 0, "outcome": "Yes", "slug": "btc-100k-eoy",
                "title": "Bitcoin closes 2026 above $100k", "event_slug": "btc-eoy-2026",
                "transaction_hash": "0xtx_btc",
            },
            # alpha's exited position — has a buy event but position is closed.
            {
                "proxy_wallet": "0xalpha", "timestamp": 1729000000,
                "condition_id": "0xcond_exit", "type": "TRADE", "side": "BUY",
                "size": 100.0, "usdc_size": 55.0, "price": 0.55,
                "outcome_index": 0, "outcome": "Yes", "slug": "exited",
                "title": "Already exited", "event_slug": "exited-event",
                "transaction_hash": "0xtx_exit",
            },
            # alpha SELL event on fed — must be ignored (we only count BUYs).
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731600000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "SELL",
                "size": 500.0, "usdc_size": 250.0, "price": 0.50,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_sell",
            },
            # noob's BUY — wallet excluded earlier so this never reaches output.
            {
                "proxy_wallet": "0xnoob", "timestamp": 1731000000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 10.0, "usdc_size": 4.5, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_noob",
            },
        ],
    )

    # Live market prices so the feed can carry current_market_yes_price.
    _write_jsonl(
        d / "polymarket_markets.jsonl",
        [
            {
                "gamma_id": "1", "condition_id": "0xcond_fed", "slug": "fed-50bp-sep",
                "question": "Fed cuts 50bp in September", "category": "economics",
                "end_date": "2026-09-18T00:00:00Z",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.58, 0.42],
                "volume_usdc": 100000, "liquidity_usdc": 5000,
                "closed": False, "active": True,
                "last_trade_price": 0.58, "best_bid": 0.57, "best_ask": 0.59,
                "fetched_at": "2026-05-12T08:00:00Z",
            },
        ],
    )

    return d


def test_skilled_bets_orders_latest_buy_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feed semantics: newest BUY entry on top; the latest of multiple buys
    on the same position is what gets shown."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    assert resp.status_code == 200
    rows = resp.json()
    # alpha has TWO held positions with matching buys; both should appear.
    assert len(rows) == 2

    # Newest first — the fed-50bp entry (ts=1731500000) ranks above btc (1730000000).
    assert rows[0]["condition_id"] == "0xcond_fed"
    assert rows[0]["bought_at"] == 1731500000
    assert rows[0]["transaction_hash"] == "0xtx_new", \
        "Should reflect the LATEST buy, not the older one"
    assert rows[1]["condition_id"] == "0xcond_btc"
    assert rows[1]["bought_at"] == 1730000000


def test_skilled_bets_excludes_low_skill_and_exited_and_unknown_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    rows = resp.json()
    wallets = {r["proxy_wallet"] for r in rows}
    # Noob fails min_skill; ghost has no matching BUY event; alpha's fully
    # exited position has size=0 in the latest snapshot.
    assert wallets == {"0xalpha"}
    conditions = {r["condition_id"] for r in rows}
    assert "0xcond_exit" not in conditions, "fully exited position must not surface"


def test_skilled_bets_carries_deep_link_urls_and_current_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row must carry working Polymarket URLs and the live market price
    so the consumer can decide whether the entry is still attractive."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    fed_row = next(r for r in resp.json() if r["condition_id"] == "0xcond_fed")

    assert fed_row["polymarket_profile_url"] == "https://polymarket.com/profile/0xalpha"
    assert fed_row["polymarket_market_url"] == "https://polymarket.com/event/fed-decision"
    assert fed_row["entry_price"] == 0.45
    # Live market has moved from 0.45 (entry) to 0.58 — caller can compute drift.
    assert fed_row["current_market_yes_price"] == 0.58
    assert fed_row["current_position_size"] == 5000.0


def test_skilled_bets_min_position_value_filters_dust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Alpha's BTC position is worth $800; raise the floor above it.
    resp = client.get(
        "/signals/skilled-bets?min_skill=0.9&min_resolved=20&min_position_value_usdc=1000"
    )
    rows = resp.json()
    # Only the fed position ($2900) clears the bar.
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "0xcond_fed"


def test_skilled_bets_surfaces_kalshi_mirror_when_match_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Central product behavior: when the Polymarket → Kalshi matcher has
    found an equivalent Kalshi market for the Polymarket condition_id the
    skilled wallet is holding, the bet must carry the Kalshi ticker,
    title, deep link, and current YES price so the user can tail it."""
    pm_dir = _seed(tmp_path)

    # Two candidate matches for the same condition_id — the approved /
    # higher-confidence one should win, and a rejected link must be
    # ignored entirely.
    _write_jsonl(
        pm_dir / "market_links.jsonl",
        [
            {
                "kalshi_ticker": "KXFED-26SEP-50BP",
                "polymarket_condition_id": "0xcond_fed",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Fed September 50bp cut",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.92,
                "status": "approved",
                "matched_by": "auto",
                "matched_at": "2026-05-12T08:00:00Z",
            },
            {
                "kalshi_ticker": "KXFED-26SEP-OLD",
                "polymarket_condition_id": "0xcond_fed",
                "polymarket_slug": "fed-50bp-sep",
                "kalshi_title": "Older candidate",
                "polymarket_title": "Fed cuts 50bp in September",
                "kalshi_end_date": "2026-09-18T00:00:00Z",
                "polymarket_end_date": "2026-09-18T00:00:00Z",
                "confidence": 0.99,
                "status": "rejected",
                "matched_by": "manual",
                "matched_at": "2026-05-12T07:00:00Z",
            },
        ],
    )
    _write_jsonl(
        pm_dir / "kalshi_markets.jsonl",
        [
            {
                "ticker": "KXFED-26SEP-50BP",
                "event_ticker": "KXFED-26SEP",
                "title": "Fed cuts 50 bps at the September meeting",
                "subtitle": "",
                "yes_sub_title": "",
                "category": "Economics",
                "status": "open",
                "expiration_time": "2026-09-18T00:00:00Z",
                "close_time": "2026-09-18T00:00:00Z",
                "yes_bid": 55,
                "yes_ask": 57,
                "last_price": 56,
                "fetched_at": "2026-05-12T08:00:00Z",
            },
        ],
    )

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    resp = client.get("/signals/skilled-bets?min_skill=0.9&min_resolved=20")
    rows = resp.json()
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")

    assert fed["kalshi_ticker"] == "KXFED-26SEP-50BP"
    assert fed["kalshi_event_ticker"] == "KXFED-26SEP"
    # Live title from kalshi_markets.jsonl (not the snapshot in market_links).
    assert fed["kalshi_title"] == "Fed cuts 50 bps at the September meeting"
    assert fed["kalshi_market_url"] == "https://kalshi.com/markets/kxfed-26sep"
    # Mid of 55/57 cents -> 0.56.
    assert fed["kalshi_yes_price"] == 0.56
    assert fed["kalshi_match_confidence"] == 0.92
    assert fed["kalshi_match_status"] == "approved"

    # The btc condition has no link → mirror fields must be empty so the UI
    # can hide the Kalshi CTA cleanly.
    btc = next(r for r in rows if r["condition_id"] == "0xcond_btc")
    assert btc["kalshi_ticker"] == ""
    assert btc["kalshi_market_url"] == ""
    assert btc["kalshi_yes_price"] == 0.0
    assert btc["kalshi_match_status"] == ""


def test_skilled_bets_empty_when_no_skilled_wallets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))

    client = TestClient(app)
    # Set bar above alpha's 0.99 → no skilled wallets → empty feed.
    resp = client.get("/signals/skilled-bets?min_skill=0.999&min_resolved=20")
    assert resp.json() == []
