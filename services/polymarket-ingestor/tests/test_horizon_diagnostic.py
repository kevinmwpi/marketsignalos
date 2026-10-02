from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import closing_lines, entry_prices
from marketsignalos_polymarket.closing_lines import ACTIVITY_FILE
from marketsignalos_polymarket.horizon_diagnostic import (
    MARKETS_FILE,
    REPORT_DIR,
    diagnose,
    resolved_winners,
    run,
)

C = entry_prices.CHUNK_SECONDS
W = 2870 * C  # 2025-01-02T00:00:00Z, a chunk boundary
HOUR = 3600
FETCHED = datetime.fromtimestamp(W + 2 * C, UTC).isoformat()


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _buy(wallet: str, cid: str, outcome: int, ts: int, price: float, usdc: float) -> dict[str, Any]:
    return {"type": "TRADE", "side": "BUY", "proxy_wallet": wallet, "condition_id": cid,
            "outcome_index": outcome, "timestamp": ts, "price": price, "usdc_size": usdc}


def _series(cid: str, prices: list[float]) -> list[dict[str, Any]]:
    return [{"condition_id": cid, "outcome_index": 0, "event_time": _iso(W + i * HOUR),
             "observed_time": FETCHED, "price": price} for i, price in enumerate(prices)]


@pytest.fixture
def pilot_dir(tmp_path: Path) -> Path:
    """Market A resolves YES and its price converges at hour 10; market B resolves NO
    and never moves; E resolves but has no price history; C is open; D is ambiguous."""
    _jsonl(tmp_path / MARKETS_FILE, [
        {"condition_id": "0xa", "closed": True, "outcome_prices": [0.6, 0.4]},  # superseded
        {"condition_id": "0xA", "closed": True, "outcome_prices": [1.0, 0.0]},
        {"condition_id": "0xb", "closed": True, "outcome_prices": ["0", "1"]},
        {"condition_id": "0xe", "closed": True, "outcome_prices": [1.0, 0.0]},
        {"condition_id": "0xc", "closed": False, "outcome_prices": [1.0, 0.0]},
        {"condition_id": "0xd", "closed": True, "outcome_prices": [0.5, 0.5]},
    ])
    _jsonl(tmp_path / ACTIVITY_FILE, [
        _buy("0xw1", "0xa", 0, W + 1800, 0.40, 10.0),
        _buy("0xw1", "0xa", 0, W + 5400, 0.50, 30.0),
        _buy("0xw2", "0xb", 0, W + 1800, 0.30, 5.0),  # loses
        _buy("0xw2", "0xb", 1, W + 1800, 0.70, 5.0),  # wins
        _buy("0xw3", "0xa", 0, W + 3 * C, 0.60, 5.0),  # window not fetched yet
        _buy("0xw3", "0xe", 0, W + 1800, 0.20, 5.0),  # fetched, no prices
        _buy("0xw4", "0xc", 0, W + 1800, 0.20, 5.0),  # unresolved
        _buy("0xw4", "0xd", 0, W + 1800, 0.20, 5.0),  # unresolved
        {**_buy("0xw5", "0xa", 0, W + 1800, 0.40, 5.0), "side": "SELL"},
    ])
    store = tmp_path / entry_prices.STORE_DIR
    _jsonl(store / entry_prices.OBSERVATIONS_FILE,
           _series("0xa", [0.45] * 10 + [0.995] * 40) + _series("0xb", [0.3] * 50))
    _jsonl(store / entry_prices.RECEIPTS_FILE, [
        {"condition_id": cid, "chunk_start": _iso(W), "chunk_end": _iso(W + C),
         "status": status, "observed_time": FETCHED}
        for cid, status in (("0xa", "ok"), ("0xb", "ok"), ("0xe", "empty"))
    ])
    _jsonl(tmp_path / closing_lines.STORE_DIR / closing_lines.OBSERVATIONS_FILE, [
        {"condition_id": "0xa", "outcome_index": 0, "event_time": _iso(W + 49 * HOUR),
         "observed_time": FETCHED, "price": 0.995},
        {"condition_id": "0xb", "outcome_index": 0, "event_time": _iso(W + 49 * HOUR),
         "observed_time": FETCHED, "price": 0.3},
    ])
    return tmp_path


def test_resolved_winners_need_a_closed_market_with_one_clear_winner(pilot_dir: Path) -> None:
    assert resolved_winners(pilot_dir / MARKETS_FILE) == {"0xa": 0, "0xb": 1, "0xe": 0}


def test_diagnose_reports_coverage_leakage_and_signal_per_horizon(pilot_dir: Path) -> None:
    report = diagnose(pilot_dir, horizons_hours=(1, 24))

    assert (report["resolved_markets"], report["resolved_bets"], report["wallets"]) == (3, 5, 3)
    one, day = report["horizons"]["1h"], report["horizons"]["24h"]
    # w3's bet on A waits on the backfill; its bet on E has a fetched, empty window.
    assert (one["pending"], one["no_reference"], one["referenced"]) == (1, 1, 3)
    assert (day["pending"], day["no_reference"], day["referenced"]) == (1, 1, 3)

    # After an hour A still trades at 0.45: (10 x 0.05 + 30 x -0.05) / 40.
    assert one["near_outcome"] == 0.0
    assert one["clv_mean_won"] == pytest.approx((-0.025 + 0.0) / 2)
    # A day later A sits at 0.995, within 0.01 of its outcome: the reference leaks.
    assert day["near_outcome"] == pytest.approx(1 / 3, abs=1e-4)
    a_clv = (10 * (0.995 - 0.40) + 30 * (0.995 - 0.50)) / 40
    expected = statistics.correlation([a_clv, 0.0, 0.0], [1.0, 0.0, 1.0])
    assert day["clv_win_corr"] == pytest.approx(expected, abs=1e-4)
    assert day["clv_mean_lost"] == 0.0
    # The pre-close price leaks for A at every horizon, on the same three bets.
    assert (one["preclose_bets"], one["near_outcome_preclose"]) == (3, pytest.approx(1 / 3, abs=1e-4))
    assert (day["wallets"], day["top_wallet_share"]) == (2, pytest.approx(2 / 3, abs=1e-4))

    assert report["common"]["bets"] == 3
    assert report["common"]["24h"]["near_outcome"] == pytest.approx(1 / 3, abs=1e-4)
    assert "pending" not in report["common"]["24h"]


def test_diagnose_has_no_gate_counts(pilot_dir: Path) -> None:
    """h is chosen before anyone sees how many wallets it would qualify."""
    text = json.dumps(diagnose(pilot_dir, horizons_hours=(1, 24)))
    assert "gate" not in text and "qualif" not in text and "tailab" not in text


def test_an_empty_data_directory_gives_an_empty_report(tmp_path: Path) -> None:
    report = diagnose(tmp_path, horizons_hours=(1,))
    assert report["resolved_bets"] == 0 and report["common"]["bets"] == 0
    assert report["horizons"]["1h"]["clv_win_corr"] is None


def test_run_writes_the_days_report(pilot_dir: Path) -> None:
    now = datetime(2026, 10, 3, 8, tzinfo=UTC)
    result = run(pilot_dir, now=now)
    assert result["status"] == "succeeded"
    saved = json.loads((pilot_dir / REPORT_DIR / "2026-10-03.json").read_text())
    assert saved["horizons"] == result["horizons"]
