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
    """One skilled wallet (alpha) holding two open, actionable positions."""
    d = tmp_path / "pmdata"
    d.mkdir(parents=True, exist_ok=True)

    _write_jsonl(
        d / "polymarket_wallet_enrichment.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "name": "AlphaWhale", "pseudonym": "AlphaWhale",
                "resolved_trades": 50, "wins": 38, "losses": 12,
                "win_rate": 0.76, "skill_likelihood": 0.99, "stddevs_above_expected": 3.7,
                "forecast_skill_likelihood": 0.99, "forecast_edge_mean": 0.5,
                "forecast_edge_lower_bound": 0.2, "independent_settled_events": 50.0,
                "all_time_pnl_usdc": 250_000, "all_time_roi": 0.25, "pnl_30d_usdc": 1_000,
                "data_quality_status": "trusted", "tailability_status": "tailable",
                "score_version": "forecast-v3",
                "total_volume_usdc": 1_000_000, "total_pnl_usdc": 250_000,
                "avg_position_size_usdc": 20_000, "trade_count": 200,
                "last_activity_at": 1731000000, "computed_at": "2026-06-09T00:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_positions.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_fed",
                "outcome_index": 0, "outcome": "Yes",
                "size": 5000.0, "avg_price": 0.45, "current_value_usdc": 2900.0,
                "slug": "fed-50bp-sep", "title": "Fed cuts 50bp in September",
                "event_slug": "fed-decision",
                "current_outcome_price": 0.58, "snapshot_id": "snapshot-alpha",
                "snapshot_at": "2026-06-09T08:00:00Z",
            },
            {
                "proxy_wallet": "0xalpha", "condition_id": "0xcond_btc",
                "outcome_index": 0, "outcome": "Yes",
                "size": 2000.0, "avg_price": 0.30, "current_value_usdc": 800.0,
                "slug": "btc-100k-eoy", "title": "Bitcoin closes 2026 above $100k",
                "event_slug": "btc-eoy-2026",
                "current_outcome_price": 0.40, "snapshot_id": "snapshot-alpha",
                "snapshot_at": "2026-06-09T08:00:00Z",
            },
        ],
    )
    _write_jsonl(
        d / "polymarket_position_snapshots.jsonl",
        [{"proxy_wallet": "0xalpha", "snapshot_id": "snapshot-alpha", "complete": True}],
    )
    _write_jsonl(
        d / "polymarket_activity.jsonl",
        [
            {
                "proxy_wallet": "0xalpha", "timestamp": 1731500000,
                "condition_id": "0xcond_fed", "type": "TRADE", "side": "BUY",
                "size": 5000.0, "usdc_size": 2250.0, "price": 0.45,
                "outcome_index": 0, "outcome": "Yes", "slug": "fed-50bp-sep",
                "title": "Fed cuts 50bp in September", "event_slug": "fed-decision",
                "transaction_hash": "0xtx_fed",
            },
            {
                "proxy_wallet": "0xalpha", "timestamp": 1730000000,
                "condition_id": "0xcond_btc", "type": "TRADE", "side": "BUY",
                "size": 2000.0, "usdc_size": 600.0, "price": 0.30,
                "outcome_index": 0, "outcome": "Yes", "slug": "btc-100k-eoy",
                "title": "Bitcoin closes 2026 above $100k", "event_slug": "btc-eoy-2026",
                "transaction_hash": "0xtx_btc",
            },
        ],
    )
    _write_jsonl(d / "polymarket_markets.jsonl", _open_markets())
    return d


def _open_markets() -> list[dict[str, object]]:
    return [
        {
            "gamma_id": "1", "condition_id": "0xcond_fed", "slug": "fed-50bp-sep",
            "question": "Fed cuts 50bp in September", "category": "economics",
            "end_date": "2026-09-18T00:00:00Z",
            "outcomes": ["Yes", "No"], "outcome_prices": [0.58, 0.42],
            "volume_usdc": 100000, "liquidity_usdc": 5000,
            "closed": False, "active": True,
            "last_trade_price": 0.58, "best_bid": 0.57, "best_ask": 0.59,
            "fetched_at": "2026-06-09T08:00:00Z",
        },
        {
            "gamma_id": "2", "condition_id": "0xcond_btc", "slug": "btc-100k-eoy",
            "question": "Bitcoin closes 2026 above $100k", "category": "crypto",
            "end_date": "2026-12-31T00:00:00Z",
            "outcomes": ["Yes", "No"], "outcome_prices": [0.40, 0.60],
            "volume_usdc": 50000, "liquidity_usdc": 2500,
            "closed": False, "active": True,
            "last_trade_price": 0.40, "best_bid": 0.39, "best_ask": 0.41,
            "fetched_at": "2026-06-09T08:00:00Z",
        },
    ]


def _resolve_fed(pm_dir: Path, *, outcome_prices: list[float]) -> None:
    """Append a newer snapshot of the fed market marking it resolved."""
    markets = _open_markets()
    markets[0].update(
        closed=True,
        active=False,
        outcome_prices=outcome_prices,
        fetched_at="2026-09-19T00:00:00Z",
    )
    _write_jsonl(pm_dir / "polymarket_markets.jsonl", markets)


def test_refresh_records_open_signals_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)

    body = client.post("/signals/ledger/refresh").json()
    assert body["recorded"] == 2
    assert body["open_rows"] == 2
    assert body["settled_won"] == 0

    # Re-running must not duplicate rows for the same still-held positions.
    body = client.post("/signals/ledger/refresh").json()
    assert body["recorded"] == 0
    assert body["total_rows"] == 2

    rows = client.get("/signals/ledger").json()
    assert len(rows) == 2
    fed = next(r for r in rows if r["condition_id"] == "0xcond_fed")
    assert fed["status"] == "open"
    assert fed["entry_price"] == 0.45
    assert fed["surface_price"] == 0.58
    assert fed["wallet_name"] == "AlphaWhale"
    assert fed["recorded_at"]
    # Feed context frozen at record time, for the summary breakdowns.
    assert fed["category"] == "economics"
    assert fed["remaining_edge_status"] == "fresh"
    assert fed["conviction"] == "unknown"
    assert fed["consensus_wallets"] == 1
    assert fed["wallet_archetype"] == "unclassified"
    # Edge bound 0.2 on entry 0.45 → fair ≈ 0.4998, now 0.58 → EV −8.0¢.
    assert fed["tail_fair_price"] == 0.4998
    assert fed["tail_ev"] == -0.0802
    assert fed["tail_ev_status"] == "negative"


def test_refresh_settles_won_signal_with_both_roi_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/ledger/refresh")

    # Fed market resolves YES — alpha's outcome 0 wins.
    _resolve_fed(pm_dir, outcome_prices=[1.0, 0.0])
    body = client.post("/signals/ledger/refresh").json()
    assert body["settled_won"] == 1
    assert body["settled_lost"] == 0
    assert body["open_rows"] == 1  # btc still open

    fed = next(
        r
        for r in client.get("/signals/ledger").json()
        if r["condition_id"] == "0xcond_fed"
    )
    assert fed["status"] == "won"
    assert fed["settled_at"]
    # $1 at the surfaced price of 0.58 → (1 - 0.58) / 0.58.
    assert fed["roi_at_surface"] == 0.7241
    # $1 at the wallet's entry of 0.45 → (1 - 0.45) / 0.45.
    assert fed["roi_at_entry"] == 1.2222


def test_refresh_settles_lost_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/ledger/refresh")

    _resolve_fed(pm_dir, outcome_prices=[0.0, 1.0])  # NO wins
    body = client.post("/signals/ledger/refresh").json()
    assert body["settled_lost"] == 1

    fed = next(
        r
        for r in client.get("/signals/ledger?status=lost").json()
        if r["condition_id"] == "0xcond_fed"
    )
    assert fed["roi_at_surface"] == -1.0
    assert fed["roi_at_entry"] == -1.0


def test_refresh_voids_canceled_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/ledger/refresh")

    # Closed without a single >=0.99 winner — canceled / ambiguous.
    _resolve_fed(pm_dir, outcome_prices=[0.5, 0.5])
    body = client.post("/signals/ledger/refresh").json()
    assert body["settled_void"] == 1

    fed = next(
        r
        for r in client.get("/signals/ledger?status=void").json()
        if r["condition_id"] == "0xcond_fed"
    )
    assert fed["roi_at_surface"] == 0.0
    assert fed["roi_at_entry"] == 0.0


def test_ledger_summary_reports_hit_rate_and_roi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/ledger/refresh")
    _resolve_fed(pm_dir, outcome_prices=[1.0, 0.0])
    client.post("/signals/ledger/refresh")

    body = client.get("/signals/ledger/summary").json()
    assert body["total_signals"] == 2
    assert body["open"] == 1
    assert body["won"] == 1
    assert body["lost"] == 0
    assert body["hit_rate"] == 1.0
    assert body["avg_roi_at_surface"] == 0.7241
    assert body["avg_roi_at_entry"] == 1.2222

    # Calibration vs the market: the one settled signal surfaced at 0.58
    # (market said 58% likely) and won — a +42pt edge over the quoted odds.
    calibration = body["calibration"]
    assert calibration["settled_priced"] == 1
    assert calibration["avg_market_implied_prob"] == 0.58
    assert calibration["hit_rate_priced"] == 1.0
    assert calibration["edge_vs_market"] == 0.42

    # Slice breakdowns keyed by the context frozen on each row.
    by_category = body["breakdowns"]["category"]
    assert by_category["economics"]["won"] == 1
    assert by_category["economics"]["edge_vs_market"] == 0.42
    assert by_category["crypto"]["signals"] == 1
    assert by_category["crypto"]["won"] == 0
    fresh = body["breakdowns"]["remaining_edge_status"]["fresh"]
    assert fresh["signals"] == 2
    assert fresh["won"] == 1
    assert body["breakdowns"]["consensus"]["1 wallet"]["signals"] == 2

    # The open btc row marks to its latest observed price (unchanged → 0).
    mtm = body["open_mark_to_market"]
    assert mtm["marked"] == 1
    assert mtm["avg_unrealized_roi_at_surface"] == 0.0


def test_ledger_summary_marks_open_rows_to_latest_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open rows shouldn't have to wait for resolution to report: they mark
    to the latest observed outcome price."""
    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    client = TestClient(app)
    client.post("/signals/ledger/refresh")

    # BTC (surfaced at 0.40) now trades at 0.50 — still open.
    markets = _open_markets()
    markets[1].update(
        outcome_prices=[0.50, 0.50],
        last_trade_price=0.50,
        fetched_at="2026-06-10T08:00:00Z",
    )
    _write_jsonl(pm_dir / "polymarket_markets.jsonl", markets)

    mtm = client.get("/signals/ledger/summary").json()["open_mark_to_market"]
    assert mtm["marked"] == 2
    # fed: unchanged at 0.58 → 0. btc: (0.50 − 0.40) / 0.40 = 0.25.
    assert mtm["avg_unrealized_roi_at_surface"] == 0.125
    assert mtm["total_unrealized_roi_at_surface"] == 0.25


def test_ledger_summary_empty_without_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path / "empty"))
    body = TestClient(app).get("/signals/ledger/summary").json()
    assert body["total_signals"] == 0
    assert body["hit_rate"] == 0.0
    assert body["calibration"]["settled_priced"] == 0
    assert body["open_mark_to_market"]["marked"] == 0
    assert body["breakdowns"]["category"] == {}
