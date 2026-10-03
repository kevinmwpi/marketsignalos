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
    HORIZONS_HOURS,
    MARKETS_FILE,
    REPORT_DIR,
    RULE,
    _missing_reason,
    diagnose,
    priority_chunks,
    priority_markets,
    resolved_winners,
    run,
    select_horizon,
)
from marketsignalos_polymarket.runner import parse_activity_row, parse_market_row
from marketsignalos_polymarket.storage import JsonlActivityStore, JsonlMarketStore

C = entry_prices.CHUNK_SECONDS
W = 2870 * C  # 2025-01-02T00:00:00Z, a chunk boundary
HOUR = 3600
FETCHED = datetime.fromtimestamp(W + 2 * C, UTC).isoformat()


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _buy(wallet: str, cid: str, outcome: int, ts: int, price: float, usdc: float,
         side: str = "BUY") -> dict[str, Any]:
    """A Data API /activity row. It goes through the real parser and store, which
    keep size and USDC but drop price, as on the pilot's volume."""
    return {"proxyWallet": wallet, "timestamp": ts, "conditionId": cid, "type": "TRADE",
            "side": side, "size": usdc / price, "usdcSize": usdc, "price": price,
            "outcomeIndex": outcome, "transactionHash": f"0xt{wallet}{cid}{outcome}{ts}{side}"}


def _market(cid: str, closed: bool, prices: list[str], end: int = W + 60 * 86400) -> dict[str, Any]:
    """A Gamma /markets row, stored through the real parser and market store. ``end``
    is the scheduled end; by default two months after the fixture's buys."""
    return {"id": cid, "conditionId": cid, "closed": closed, "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps(prices), "endDate": _iso(end)}


def _series(cid: str, prices: list[float]) -> list[dict[str, Any]]:
    return [{"condition_id": cid, "outcome_index": 0, "event_time": _iso(W + i * HOUR),
             "observed_time": FETCHED, "price": price} for i, price in enumerate(prices)]


@pytest.fixture
def pilot_dir(tmp_path: Path) -> Path:
    """Market A resolves YES and its price converges at hour 10; market B resolves NO
    and never moves; E resolves but has no price history; C is open; D is ambiguous."""
    markets = JsonlMarketStore(tmp_path / MARKETS_FILE)
    markets.write_markets([parse_market_row(_market("0xa", False, ["0.5", "0.5"]))])
    markets.write_markets([parse_market_row(row) for row in (
        _market("0xa", True, ["1", "0"]),  # replaces the open row above
        _market("0xb", True, ["0", "1"]),
        _market("0xe", True, ["1", "0"]),
        _market("0xc", False, ["1", "0"]),
        _market("0xd", True, ["0.5", "0.5"]),
        _market("0xf", True, ["1", "0"], end=W + 2 * 86400),  # ends two days after its buy
    )])
    activity = JsonlActivityStore(tmp_path / ACTIVITY_FILE)
    activity.write_activity([parse_activity_row(row) for row in (
        _buy("0xw1", "0xa", 0, W + 1800, 0.40, 10.0),
        _buy("0xw1", "0xa", 0, W + 5400, 0.50, 30.0),
        _buy("0xw2", "0xb", 0, W + 1800, 0.30, 5.0),  # loses
        _buy("0xw2", "0xb", 1, W + 1800, 0.70, 5.0),  # wins
        _buy("0xw3", "0xa", 0, W + 3 * C, 0.60, 5.0),  # window not fetched yet
        _buy("0xw3", "0xe", 0, W + 1800, 0.20, 5.0),  # fetched, no prices
        _buy("0xw4", "0xc", 0, W + 1800, 0.20, 5.0),  # unresolved
        _buy("0xw4", "0xd", 0, W + 1800, 0.20, 5.0),  # unresolved
        _buy("0xw4", "0xf", 0, W + 1800, 0.20, 5.0),  # resolved, but too close to its end
        _buy("0xw5", "0xa", 0, W + 1800, 0.40, 5.0, side="SELL"),
    )])
    activity.flush()
    stored = json.loads((tmp_path / ACTIVITY_FILE).read_text().splitlines()[0])
    assert "price" not in stored  # the case that hid a bug: fills carry no price
    store = tmp_path / entry_prices.STORE_DIR
    _jsonl(store / entry_prices.OBSERVATIONS_FILE,
           _series("0xa", [0.45] * 10 + [0.995] * 40) + _series("0xb", [0.3] * 50))
    _jsonl(store / entry_prices.RECEIPTS_FILE, [
        {"condition_id": cid, "chunk_start": _iso(W), "chunk_end": _iso(W + C),
         "status": status, "observed_time": FETCHED}
        for cid, status in (("0xa", "ok"), ("0xb", "ok"), ("0xe", "empty"))
    ])
    _jsonl(tmp_path / closing_lines.STORE_DIR / closing_lines.RECEIPTS_FILE, [
        {"condition_id": "0xa", "status": "ok", "observed_time": FETCHED},
        {"condition_id": "0xc", "status": "not_closed", "observed_time": FETCHED},
    ])
    _jsonl(tmp_path / closing_lines.STORE_DIR / closing_lines.OBSERVATIONS_FILE, [
        {"condition_id": "0xa", "outcome_index": 0, "event_time": _iso(W + 49 * HOUR),
         "observed_time": FETCHED, "price": 0.995},
        {"condition_id": "0xb", "outcome_index": 0, "event_time": _iso(W + 49 * HOUR),
         "observed_time": FETCHED, "price": 0.3},
    ])
    return tmp_path


def test_resolved_winners_need_a_closed_market_with_one_clear_winner(pilot_dir: Path) -> None:
    assert resolved_winners(pilot_dir / MARKETS_FILE) == {"0xa": 0, "0xb": 1, "0xe": 0, "0xf": 0}


def test_diagnose_reports_coverage_leakage_and_signal_per_horizon(pilot_dir: Path) -> None:
    report = diagnose(pilot_dir, horizons_hours=(1, 24))

    assert (report["resolved_markets"], report["resolved_bets"], report["wallets"]) == (4, 5, 3)
    # Nine BUY fills on six markets; C is open and D has no clear winner. F resolved,
    # but its only buy came two days before its scheduled end, inside the 7-day rule.
    assert report["funnel"] == {"buy_fills": 9, "bought_markets": 6, "in_market_store": 6,
                                "closed_in_store": 5, "resolved_in_store": 4,
                                "closed_per_gamma_lookup": 1, "resolved_bets": 6,
                                "bets_7d_before_scheduled_end": 5, "bets_7d_close_known": 0,
                                "bets_7d_closed_within_7d": 0}
    one, day = report["horizons"]["1h"], report["horizons"]["24h"]
    # w3's bet on A waits on the backfill; its bet on E has a fetched, empty window,
    # and no known close time explains why.
    assert (one["pending"], one["no_reference"], one["referenced"]) == (1, 1, 3)
    assert (day["pending"], day["no_reference"], day["referenced"]) == (1, 1, 3)
    assert one["no_reference_reasons"] == day["no_reference_reasons"] == {"ended": 1}

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
    assert one["coverage"] == 0.75  # 3 referenced of 4 fetched; the pending bet is excluded
    assert report["selection"]["eligible"] is False  # 3 bets is far below the rule's 500


def test_known_close_times_explain_missing_references_and_check_the_end_date_filter(
    pilot_dir: Path,
) -> None:
    """E closed an hour after its buy although its scheduled end was two months off;
    A's actual close (from an entry-price receipt) came 30 days in."""
    with (pilot_dir / closing_lines.STORE_DIR / closing_lines.RECEIPTS_FILE).open("a") as out:
        out.write(json.dumps({"condition_id": "0xe", "status": "ok", "observed_time": FETCHED,
                              "close_time": _iso(W + HOUR), "close_field": "closedTime"}) + "\n")
        out.write(json.dumps({"condition_id": "0xb", "status": "ok", "observed_time": FETCHED,
                              "close_time": _iso(W + HOUR), "close_field": "endDate"}) + "\n")
    with (pilot_dir / entry_prices.STORE_DIR / entry_prices.RECEIPTS_FILE).open("a") as out:
        out.write(json.dumps({"condition_id": "0xa", "chunk_start": _iso(W + C),
                              "chunk_end": _iso(W + 2 * C), "status": "ok",
                              "observed_time": FETCHED,
                              "market_closed_time": _iso(W + 30 * 86400)}) + "\n")

    report = diagnose(pilot_dir, horizons_hours=(1, 24))
    assert report["horizons"]["1h"]["no_reference_reasons"] == {"closed": 1}
    # A scheduled end date is not a close: B stays unknown. Of A's two bets and E's
    # one, only E closed within seven days of its first fill.
    assert (report["funnel"]["bets_7d_close_known"],
            report["funnel"]["bets_7d_closed_within_7d"]) == (3, 1)


def test_a_missing_reference_is_a_close_an_ended_series_or_a_gap() -> None:
    series = [(W + i * HOUR, 0.5) for i in (0, 1, 2, 30)]
    assert _missing_reason(series, W, 24 * HOUR, closed_at=None) == "gap"
    assert _missing_reason(series, W, 40 * HOUR, closed_at=None) == "ended"
    assert _missing_reason(series, W, 40 * HOUR, closed_at=W + 40 * HOUR) == "closed"
    assert _missing_reason(series, W, 24 * HOUR, closed_at=W + 25 * HOUR) == "gap"
    assert _missing_reason([], W, HOUR, closed_at=None) == "ended"


def test_priority_is_the_windows_and_markets_of_the_bets_the_rule_counts(pilot_dir: Path) -> None:
    # Unresolved C and D, and F's buy two days before its end, are left out. Every
    # window ends six hours after its buy, inside the buy's own chunk.
    assert priority_chunks(pilot_dir) == {
        ("0xa", W), ("0xa", W + 3 * C), ("0xb", W), ("0xe", W)}
    assert priority_chunks(pilot_dir) <= entry_prices.needed_chunks(pilot_dir / ACTIVITY_FILE)
    assert priority_markets(pilot_dir) == {"0xa", "0xb", "0xe"}


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


# ── The approved selection rule ──────────────────────────────────────────────

def _report(per_h: dict[str, tuple[float, float, float]], bets: int = 600,
            wallets: int = 25) -> dict[str, Any]:
    """A report with (near_outcome, coverage, clv_win_corr) per horizon."""
    common: dict[str, Any] = {"bets": bets}
    horizons: dict[str, Any] = {}
    for key, (near, coverage, corr) in per_h.items():
        common[key] = {"near_outcome": near, "clv_win_corr": corr, "wallets": wallets}
        horizons[key] = {"coverage": coverage}
    return {"common": common, "horizons": horizons}


GOOD = (0.01, 0.9, 0.2)


@pytest.mark.parametrize(("bets", "wallets"), [(499, 25), (600, 19)])
def test_the_rule_waits_for_500_bets_from_20_wallets(bets: int, wallets: int) -> None:
    selection = select_horizon(_report({"24h": GOOD}, bets, wallets), (24,))
    assert selection["eligible"] is False and "needs 500 from 20" in selection["reason"]


def test_the_rule_takes_the_longest_horizon_that_passes_every_condition() -> None:
    report = _report({"168h": (0.20, 0.9, 0.3), "72h": (0.02, 0.4, 0.3),
                      "24h": (0.03, 0.8, 0.0), "6h": (0.05, 0.5, 0.01), "1h": GOOD})
    selection = select_horizon(report, (1, 6, 24, 72, 168))
    assert selection == {"eligible": True, "horizon": "6h", "rejected_longer": {
        "168h": "near_outcome 0.2", "72h": "coverage 0.4", "24h": "clv_win_corr 0.0"}}


def test_the_rule_can_find_no_horizon() -> None:
    selection = select_horizon(_report({"24h": (0.5, 0.9, 0.3), "1h": (0.0, 0.9, -0.1)}),
                               (1, 24))
    assert selection["eligible"] is True and selection["horizon"] is None
    assert set(selection["rejected_longer"]) == {"24h", "1h"}
    assert RULE == {"min_hours_to_scheduled_end": 168, "min_common_bets": 500,
                    "min_common_wallets": 20, "max_near_outcome": 0.05, "min_coverage": 0.5,
                    "min_clv_win_corr": 0.0}
    # Cut from (1, 6, 24, 72, 168) on 2026-10-03; the backfill fetches far enough.
    assert HORIZONS_HOURS == (1, 6)
    assert max(HORIZONS_HOURS) * HOUR <= entry_prices.HORIZON_SECONDS


def test_the_first_eligible_report_decides_and_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import horizon_diagnostic

    selections = iter([
        {"eligible": False, "reason": "too few"},
        {"eligible": True, "horizon": "24h", "rejected_longer": {}},
        {"eligible": True, "horizon": "6h", "rejected_longer": {}},
    ])

    def fake_diagnose(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
        assert now is not None
        return {"generated_at": now.isoformat(), "selection": next(selections)}

    monkeypatch.setattr(horizon_diagnostic, "diagnose", fake_diagnose)
    decision = tmp_path / REPORT_DIR / "decision.json"

    assert run(tmp_path, now=datetime(2026, 10, 3, tzinfo=UTC))["decision"] is None
    assert not decision.exists()
    second = run(tmp_path, now=datetime(2026, 10, 4, tzinfo=UTC))
    assert second["decision"] == {"decided_at": "2026-10-04T00:00:00+00:00", "eligible": True,
                                  "horizon": "24h", "rejected_longer": {}}
    third = run(tmp_path, now=datetime(2026, 10, 5, tzinfo=UTC))
    assert third["selection"]["horizon"] == "6h"  # today's numbers still reported
    assert third["decision"]["horizon"] == "24h"  # but the decision stands
    saved = json.loads(decision.read_text())
    assert saved["rule"] == RULE and saved["horizons_hours"] == [1, 6]
