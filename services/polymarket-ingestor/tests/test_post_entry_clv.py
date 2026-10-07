from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import entry_prices, post_entry_clv
from marketsignalos_polymarket import score_snapshot as snapshot
from marketsignalos_polymarket.post_entry_clv import HORIZON_SECONDS, bet_clv
from marketsignalos_polymarket.runner import parse_activity_row, parse_market_row
from marketsignalos_polymarket.storage import JsonlActivityStore, JsonlMarketStore

C = entry_prices.CHUNK_SECONDS
W = 2870 * C  # 2025-01-02T00:00:00Z, a chunk boundary
HOUR = 3600
DAY = 86400
WALLET = "0x" + "1" * 40


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


# ── One bet ───────────────────────────────────────────────────────────────────

SERIES = {"0xa": [(W + i * HOUR, 0.45) for i in range(10)]}
FAR_END = W + 60 * DAY


def test_clv_is_the_price_an_hour_later_minus_the_price_paid_weighted_by_usdc() -> None:
    result = bet_clv([(W + 1800, 0.40, 10.0), (W + 5400, 0.50, 30.0)], condition_id="0xa",
                     outcome_index=0, scheduled_end=FAR_END, series=SERIES)
    assert result.clv == pytest.approx((10 * 0.05 + 30 * -0.05) / 40)
    assert result.weight_usdc == 40.0 and not result.exclusions
    # The NO side reads 1 - YES: bought at 0.50, worth 0.55 an hour later.
    no = bet_clv([(W + 1800, 0.50, 5.0)], condition_id="0xa", outcome_index=1,
                 scheduled_end=FAR_END, series=SERIES)
    assert no.clv == pytest.approx(0.05)
    assert HORIZON_SECONDS == HOUR  # decision 6
    assert HORIZON_SECONDS <= entry_prices.HORIZON_SECONDS  # the backfill fetches far enough


def test_fills_without_a_usable_reference_are_counted_never_zero_filled() -> None:
    fills = [
        (W + 1800, 0.40, 10.0),                     # counts
        (FAR_END - 6 * DAY, 0.40, 10.0),            # placed under seven days before the end
        (W + 1800, 1.0, 10.0),                      # not a price strictly inside (0, 1)
        (W + 20 * HOUR, 0.40, 10.0),                # the series has nothing an hour later
    ]
    result = bet_clv(fills, condition_id="0xa", outcome_index=0, scheduled_end=FAR_END,
                     series=SERIES)
    assert result.clv == pytest.approx(0.05) and result.weight_usdc == 10.0
    assert result.exclusions == Counter(
        {"near_scheduled_end": 1, "invalid_fill": 1, "no_reference": 1})
    unknown_end = bet_clv(fills[:1], condition_id="0xa", outcome_index=0, scheduled_end=None,
                          series=SERIES)
    assert unknown_end.clv is None and unknown_end.exclusions == Counter(
        {"no_scheduled_end": 1})
    assert bet_clv([], condition_id="0xa", outcome_index=0, scheduled_end=FAR_END,
                   series=SERIES).clv is None


def test_the_fill_filter_is_the_one_the_horizon_rule_measured() -> None:
    from marketsignalos_polymarket.horizon_diagnostic import RULE

    assert post_entry_clv.MIN_SECONDS_TO_END == RULE["min_hours_to_scheduled_end"] * HOUR


# ── The scorer, end to end through the real stores ────────────────────────────

def _buy(cid: str, outcome: int, ts: int, price: float, usdc: float) -> dict[str, Any]:
    return {"proxyWallet": WALLET, "timestamp": ts, "conditionId": cid, "type": "TRADE",
            "side": "BUY", "size": usdc / price, "usdcSize": usdc, "price": price,
            "outcomeIndex": outcome, "eventSlug": f"event-{cid}",
            "transactionHash": f"0xt{cid}{outcome}{ts}"}


def _market(cid: str, closed: bool, prices: list[str], end: int) -> dict[str, Any]:
    return {"id": cid, "conditionId": cid, "closed": closed, "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps(prices), "endDate": _iso(end)}


def _observations(cid: str, price: float, observed: str) -> list[dict[str, Any]]:
    return [{"condition_id": cid, "outcome_index": 0, "event_time": _iso(W + i * HOUR),
             "observed_time": observed, "price": price} for i in range(10)]


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A resolved bet and an open one with references, a bet two days before its
    market's end, and one whose only prices were fetched after scoring began."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = tmp_path / "data"
    root.mkdir()
    markets = JsonlMarketStore(root / "polymarket_markets.jsonl")
    markets.write_markets([parse_market_row(row) for row in (
        _market("0xa", True, ["1", "0"], FAR_END),
        _market("0xb", False, ["0.3", "0.7"], FAR_END),
        _market("0xc", True, ["1", "0"], W + 2 * DAY),
        _market("0xd", True, ["0", "1"], FAR_END),
    )])
    activity = JsonlActivityStore(root / "polymarket_activity.jsonl")
    activity.write_activity([parse_activity_row(row) for row in (
        _buy("0xa", 0, W + 1800, 0.40, 10.0),
        _buy("0xa", 0, W + 5400, 0.50, 30.0),
        _buy("0xb", 1, W + 1800, 0.70, 7.0),
        _buy("0xc", 0, W + 1800, 0.40, 5.0),
        _buy("0xd", 0, W + 1800, 0.40, 5.0),
    )])
    activity.flush()
    assert "price" not in json.loads(
        (root / "polymarket_activity.jsonl").read_text().splitlines()[0])
    store = root / entry_prices.STORE_DIR
    store.mkdir()
    fetched = _iso(W + 2 * C)
    rows = (_observations("0xa", 0.45, fetched) + _observations("0xb", 0.25, fetched)
            + _observations("0xc", 0.45, fetched)
            + _observations("0xd", 0.45, "2999-01-01T00:00:00+00:00"))
    (store / entry_prices.OBSERVATIONS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return root


def _scored(data: Path, snapshots: Path, run_id: str, version: str) -> dict[str, Any]:
    snapshot.score_snapshot(data, snapshots, run_id, score_version=version)
    rows = [json.loads(line) for line in
            (snapshots / run_id / snapshot.ENRICHMENT).read_text().splitlines()]
    bets = [json.loads(line) for line in
            (snapshots / run_id / snapshot.BETS).read_text().splitlines()]
    assert len(rows) == 1
    return {"row": rows[0], "bets": {bet["condition_id"]: bet for bet in bets}}


def test_forecast_v5_scores_clv_an_hour_after_each_buy(data: Path, tmp_path: Path) -> None:
    v5 = _scored(data, tmp_path / "snapshots", "v5", "forecast-v5")
    row, bets = v5["row"], v5["bets"]
    assert row["score_version"] == "forecast-v5"
    resolved = (10 * (0.45 - 0.40) + 30 * (0.45 - 0.50)) / 40
    open_no_side = 0.75 - 0.70  # NO reads 1 - 0.25
    assert bets["0xa"]["clv"] == pytest.approx(resolved)
    assert bets["0xa"]["status"] == "won" and bets["0xb"]["status"] == "open"
    assert bets["0xb"]["clv"] == pytest.approx(open_no_side)
    assert bets["0xc"]["clv"] is None  # placed two days before its end
    assert bets["0xd"]["clv"] is None  # its prices were fetched after scoring began
    # One event-capped vote per event: two bets with a reference.
    assert row["clv_sample_size"] == pytest.approx(2.0)
    assert row["clv_mean"] == pytest.approx((resolved + open_no_side) / 2, abs=1e-6)
    assert "fewer than 10 1 h post-entry CLV observations" in row["tailability_reasons"]


def _without_timestamps(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "computed_at"}


def test_v4_ignores_entry_prices_and_v5_rescoring_is_identical(
    data: Path, tmp_path: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    v4 = _scored(data, snapshots, "v4", "forecast-v4")["row"]
    assert v4["score_version"] == "forecast-v4"
    assert "closing-line observations" in " ".join(v4["tailability_reasons"])
    store = data / entry_prices.STORE_DIR
    moved = tmp_path / "moved-entry-prices"
    store.rename(moved)
    v4_without = _scored(data, snapshots, "v4-without", "forecast-v4")["row"]
    assert _without_timestamps(v4_without) == _without_timestamps(v4)
    moved.rename(store)
    first = _scored(data, snapshots, "v5-first", "forecast-v5")
    second = _scored(data, snapshots, "v5-second", "forecast-v5")
    assert _without_timestamps(first["row"]) == _without_timestamps(second["row"])
    assert ({cid: _without_timestamps(bet) for cid, bet in first["bets"].items()}
            == {cid: _without_timestamps(bet) for cid, bet in second["bets"].items()})


def test_v5_fetches_the_hour_after_every_long_dated_fill_first(data: Path) -> None:
    # Resolved, open and not-yet-priced bets alike; 0xc came two days before its end.
    assert post_entry_clv.priority_chunks(data) == {("0xa", W), ("0xb", W), ("0xd", W)}
    assert post_entry_clv.priority_chunks(data) <= entry_prices.needed_chunks(
        data / "polymarket_activity.jsonl")


# ── The power diagnostic (plan step 3) ──────────────────────────────────────

def test_wallet_stats_report_the_sample_needed_for_a_positive_bound() -> None:
    from marketsignalos_polymarket.gate13_power import wallet_stats

    stats = wallet_stats([(0.02, "a", 5.0), (0.04, "b", 9.0)])
    assert (stats["bets"], stats["n_eff"], stats["mean"], stats["sd"]) == (
        2, 2.0, pytest.approx(0.03), pytest.approx(0.01))
    assert stats["lower_bound"] == pytest.approx(0.03 - 1.6448536 * 0.01 / 2 ** 0.5, abs=1e-6)
    assert stats["needed_n_eff"] == pytest.approx((1.6448536 * 0.01 / 0.03) ** 2, abs=0.05)
    assert stats["passes"] is False  # a sample of 2 events is under MIN_CLV_SAMPLE
    assert wallet_stats([(-0.01, "a", 1.0), (0.0, "b", 1.0)])["needed_n_eff"] is None


def test_the_power_diagnostic_counts_both_versions_and_explains_exclusions(
    data: Path, tmp_path: Path,
) -> None:
    from marketsignalos_polymarket import gate13_power

    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    result = gate13_power.run(data, now=now)
    assert result["status"] == "succeeded"
    assert result["counts"]["forecast-v4"]["wallets"] == 1
    assert result["counts"]["forecast-v5"] == {
        "wallets": 1, "gate13_pass": 0, "tailable": 0,
        "blocked_only_by_gate13": 0, "blocked_only_by_min_sample": 0}
    # 0xc was bought two days before its end; 0xd's prices were fetched after the
    # run began, and with no chunk receipts its hour counts as not yet fetched.
    assert result["exclusions"] == {"near_scheduled_end": 1, "not_fetched": 1}
    assert result["cross_check_mismatches"] == 0  # agrees with the v5 generation
    assert result["wallets_with_observations"] == 1
    saved = json.loads((data / gate13_power.REPORT_DIR / "2026-10-05.json").read_text())
    assert saved["wallets"][WALLET]["bets"] == 2
    assert saved["v5_blocked_only_by_min_sample"] == []
    assert not (data / gate13_power.SCRATCH_DIR).exists()
    assert not (data / "score-snapshots").exists()  # the published scores are untouched


def test_an_unfetchable_hour_is_split_by_why_it_has_no_price() -> None:
    from marketsignalos_polymarket.gate13_power import _no_reference_reason

    points = [(W + i * HOUR, 0.5) for i in (0, 1, 2, 30)]
    receipts = {("0xa", W): "ok"}
    later = W + 10 * C  # every chunk below has ended

    def reason(ts: int, closed_at: int | None = None, *, now: int = later,
               known: dict[tuple[str, int], str] = receipts) -> str:
        return _no_reference_reason("0xa", ts, points, known, closed_at, now=now)

    # The hour crosses into the next chunk, which was never tried: real backlog.
    assert reason(W + C - 1800) == "not_fetched"
    # The same hour while that chunk is still running (or settling) cannot be fetched.
    assert reason(W + C - 1800, now=W + 2 * C) == "not_ended"
    assert reason(W + C - 1800, now=W + C + 60) == "not_ended"
    # Tried and failed (no YES token, an HTTP error): waits for its retry.
    assert reason(W + C - 1800, known={**receipts, ("0xa", W + C): "no_token"}) == "fetch_failed"
    assert reason(W + 5 * HOUR) == "gap"
    assert reason(W + 40 * HOUR) == "ended"
    assert reason(W + 5 * HOUR, W + 5 * HOUR) == "closed"


def test_wallets_blocked_only_by_the_sample_minimum_need_a_positive_bound() -> None:
    from marketsignalos_polymarket.gate13_power import _blocked_only_by_min_sample, _counts

    few = "fewer than 10 1 h post-entry CLV observations"
    rows = [
        {"tailability_reasons": [few], "clv_lower_bound": 0.002},           # counts
        {"tailability_reasons": [few], "clv_lower_bound": -0.001},          # bound not there
        {"tailability_reasons": [few, "recent forecast edge is negative"],
         "clv_lower_bound": 0.002},                                         # another gate fails
        {"tailability_reasons": ["fewer than 10 resolved trades"],
         "clv_lower_bound": 0.002},                                         # not gate 13
        {"tailability_reasons": ["1 h post-entry CLV value not confidently positive"],
         "clv_lower_bound": -0.001, "clv_sample_size": 12.0},
    ]
    assert [_blocked_only_by_min_sample(row) for row in rows] == [True, False, False, False,
                                                                 False]
    assert _counts(rows)["blocked_only_by_min_sample"] == 1
    assert _counts(rows)["blocked_only_by_gate13"] == 3


def test_an_unknown_score_version_is_refused(data: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="score_version"):
        snapshot.score_snapshot(data, tmp_path / "snapshots", "bad", score_version="v6")


def test_compacting_the_entry_prices_changes_no_v5_score(data: Path, tmp_path: Path) -> None:
    # Owner's approval, 2026-10-06: compress the store only if v5 reads it identically.
    snapshots = tmp_path / "snapshots"
    before = _scored(data, snapshots, "v5-plain", "forecast-v5")
    store = data / entry_prices.STORE_DIR
    assert entry_prices.compact_observations(store) > 0
    assert not (store / entry_prices.OBSERVATIONS_FILE).exists()
    after = _scored(data, snapshots, "v5-archived", "forecast-v5")
    assert _without_timestamps(after["row"]) == _without_timestamps(before["row"])
    assert ({cid: _without_timestamps(bet) for cid, bet in after["bets"].items()}
            == {cid: _without_timestamps(bet) for cid, bet in before["bets"].items()})
    # The generation's inventory records the archive it read.
    manifest = json.loads((snapshots / "v5-archived" / "manifest.json").read_text())
    archive = f"{entry_prices.STORE_DIR}/{entry_prices.ARCHIVE_FILE}"
    assert manifest["inputs"][archive]["exists"]


def test_compacting_the_activity_changes_no_v5_score(data: Path, tmp_path: Path) -> None:
    # Stage 3 step 0: activity moves into gzip segments; every reader must see the
    # same rows, so scores and the entry-price work list stay identical.
    from marketsignalos_polymarket import gate13_power, jsonl_archive

    snapshots = tmp_path / "snapshots"
    activity = data / "polymarket_activity.jsonl"
    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    before = _scored(data, snapshots, "v5-plain", "forecast-v5")
    chunks = entry_prices.needed_chunks(activity)
    priority = post_entry_clv.priority_chunks(data)
    diagnosis = gate13_power.diagnose(data, observed_before=now)

    assert jsonl_archive.compact(activity) > 0
    assert activity.stat().st_size == 0 and jsonl_archive.segment_paths(activity)
    after = _scored(data, snapshots, "v5-archived", "forecast-v5")
    assert _without_timestamps(after["row"]) == _without_timestamps(before["row"])
    assert ({cid: _without_timestamps(bet) for cid, bet in after["bets"].items()}
            == {cid: _without_timestamps(bet) for cid, bet in before["bets"].items()})
    assert entry_prices.needed_chunks(activity) == chunks
    assert post_entry_clv.priority_chunks(data) == priority
    assert gate13_power.diagnose(data, observed_before=now) == diagnosis
    manifest = json.loads((snapshots / "v5-archived" / "manifest.json").read_text())
    assert manifest["inputs"]["polymarket_activity.jsonl.archive"]["files"] == 1
