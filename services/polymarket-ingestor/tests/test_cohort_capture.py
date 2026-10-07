from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket import cohort_capture
from marketsignalos_polymarket import lean_pilot as pilot
from marketsignalos_polymarket.cohort_capture import (
    activity_offset,
    appended_rows,
    capture,
    taker_fee,
    walk_asks,
)
from marketsignalos_polymarket.runner import parse_activity_row
from marketsignalos_polymarket.storage import JsonlActivityStore

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
T2 = "0x" + "a" * 40
COMPARISON = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
C1, C2, C3, C4 = "0xc1", "0xc2", "0xc3", "0xc4"


def _fill(wallet: str, cid: str, ago: int, *, side: str = "BUY", index: int = 0,
          tx: str = "") -> Any:
    return parse_activity_row({
        "proxyWallet": wallet, "timestamp": int(NOW.timestamp()) - ago, "conditionId": cid,
        "type": "TRADE", "side": side, "size": 50, "usdcSize": 25, "price": 0.5,
        "outcomeIndex": index, "eventSlug": f"event-{cid}",
        "transactionHash": tx or f"0xt{wallet[-4:]}{cid}{ago}{side}"})


@pytest.fixture
def data(tmp_path: Path) -> Path:
    stage = tmp_path / "cohort-v1"
    stage.mkdir()
    (stage / "members.json").write_text(json.dumps({
        "cohort_id": "cohort-v1", "config_hash": "h1", "mode": "provisional",
        "t2": [T2], "comparison": [COMPARISON], "wallets": [T2, COMPARISON]}))
    return tmp_path


def _append(data: Path, fills: list[Any]) -> None:
    store = JsonlActivityStore(data / "polymarket_activity.jsonl")
    store.write_activity(fills)
    store.flush()


class Venue:
    """Gamma and CLOB responses keyed by path; a value that is an exception raises."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, params: dict[str, Any]) -> Any:
        key = url.rsplit(".com", 1)[1]
        if key == "/book":
            key = f"/book/{params['token_id']}"
        self.calls.append(key)
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value


def _tokens(cid: str) -> list[dict[str, str]]:
    return [{"t": f"{cid}-yes", "o": "Yes"}, {"t": f"{cid}-no", "o": "No"}]


def _venue(**overrides: Any) -> Venue:
    responses: dict[str, Any] = {
        "/markets": [{"conditionId": C1, "feesEnabled": True,
                      "feeSchedule": {"rate": "0.05", "exponent": 1},
                      "clobTokenIds": json.dumps([f"{C1}-yes", f"{C1}-no"])},
                     {"conditionId": C2, "feesEnabled": False}],
        f"/clob-markets/{C1}": {"c": C1, "t": _tokens(C1), "ao": True,
                                "fd": {"r": 0.05, "e": 1, "to": True}},
        f"/clob-markets/{C2}": {"c": C2, "t": _tokens(C2), "ao": True},
        f"/book/{C1}-yes": {"timestamp": "1760961600000", "hash": "h",
                            "asks": [{"price": "0.52", "size": "1000"},
                                     {"price": "0.50", "size": "100"}],
                            "bids": [{"price": "0.48", "size": "10"}]},
        f"/book/{C2}-no": {"asks": [{"price": "0.30", "size": "1000"}], "bids": []},
    }
    responses.update(overrides)
    return Venue(responses)


def _signals(data: Path) -> list[dict[str, Any]]:
    path = data / "cohort-v1" / "signals.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


# ── Costs ────────────────────────────────────────────────────────────────────

def test_the_fee_matches_polymarkets_worked_example() -> None:
    # 6 shares at 0.64 on a crypto market (rate 0.072): $0.0995 (docs, 2026).
    assert taker_fee(6, 0.64, rate=0.072, exponent=1) == pytest.approx(0.0995, abs=1e-4)
    assert taker_fee(100, 0.5, rate=0.0, exponent=1) == 0.0
    # The exponent applies to p(1 - p), as in clob-client-v2.
    assert taker_fee(100, 0.5, rate=0.25, exponent=2) == pytest.approx(100 * 0.25 * 0.0625)


def test_the_clip_walks_the_asks_cheapest_first_and_pays_the_fee_on_top() -> None:
    clip = walk_asks([(0.52, 1000), (0.50, 100)], 100.0, rate=0.05, exponent=1)
    assert clip is not None
    shares = 100 + 50 / 0.52  # $50 at 0.50, then $50 at 0.52
    fee = 100 * 0.05 * 0.25 + (50 / 0.52) * 0.05 * 0.52 * 0.48
    assert clip["shares"] == pytest.approx(shares, abs=1e-6)
    assert clip["fee_usdc"] == pytest.approx(fee, abs=1e-6)
    assert clip["all_in_price"] == pytest.approx((100 + fee) / shares, abs=1e-6)
    assert clip["vwap"] == pytest.approx(100 / shares, abs=1e-6) and clip["levels"] == 2
    assert walk_asks([(0.5, 10)], 100.0, rate=0.0, exponent=1) is None  # $5 of depth


# ── Capture ──────────────────────────────────────────────────────────────────

def test_only_this_runs_fresh_member_buys_become_signals(data: Path) -> None:
    _append(data, [_fill(T2, C4, 60)])  # before the offset: a previous run's row
    offset = activity_offset(data)
    _append(data, [
        _fill(T2, C1, 600), _fill(T2, C1, 300),  # two fills, one signal
        _fill(COMPARISON, C2, 1200, index=1),
        _fill(T2, C3, 5 * 3600),  # too old to follow
        _fill(T2, C1, 200, side="SELL"), _fill(OTHER, C1, 100),
    ])
    venue = _venue()
    result = capture(data, offset, "run1", get=venue.get, now_fn=lambda: NOW)
    assert result == {"status": "succeeded", "signals": 2, "stale_fills": 1, "captured": 2,
                      "excluded": {}, "seconds": 0.0}
    first, second = _signals(data)
    assert (first["wallet"], first["groups"], first["fills"]) == (COMPARISON, ["comparison"], 1)
    assert (second["wallet"], second["groups"], second["fills"]) == (T2, ["t2"], 2)
    assert second["wallet_usdc"] == 50 and second["detection_lag_seconds"] == 300
    assert second["signal_id"] == f"run1:{T2}:{C1}:0" and second["config_hash"] == "h1"
    assert second["membership_mode"] == "provisional" and second["status"] == "captured"
    assert second["book"]["best_ask"] == 0.50 and second["book"]["best_bid"] == 0.48
    assert second["fee"]["rate"] == 0.05 and second["fee"]["gamma_fees_enabled"] is True
    assert second["clip"]["fee_usdc"] > 0
    # No fd and Gamma says fees are off: the official client charges nothing.
    assert first["fee"]["details_present"] is False and first["clip"]["fee_usdc"] == 0
    assert first["token_id"] == f"{C2}-no" and first["token_outcome"] == "No"
    assert venue.calls.count("/markets") == 1  # one Gamma batch for both markets


@pytest.mark.parametrize(("override", "reason"), [
    ({f"/book/{C1}-yes": httpx.ConnectError("down")}, "no book: request failed"),
    ({f"/book/{C1}-yes": {"asks": [], "bids": []}}, "no book: no asks"),
    ({f"/book/{C1}-yes": {"asks": [{"price": "0.5", "size": "10"}]}},
     "book too thin for the clip"),
    ({f"/clob-markets/{C1}": httpx.ConnectError("down")},
     "no fee details: CLOB market lookup failed"),
    ({f"/clob-markets/{C1}": {"c": C1, "t": _tokens(C1)}},  # Gamma says fees are on
     "fee sources disagree: Gamma feesEnabled vs CLOB fd"),
    ({f"/clob-markets/{C1}": {"c": C1, "t": _tokens(C1), "ao": False,
                              "fd": {"r": 0.05, "e": 1}}}, "market not accepting orders"),
    ({f"/clob-markets/{C1}": {"c": C1, "t": [{"t": f"{C1}-no", "o": "No"},
                                              {"t": f"{C1}-yes", "o": "Yes"}],
                              "fd": {"r": 0.05, "e": 1}}},
     "token sources disagree: Gamma clobTokenIds vs CLOB t"),
])
def test_a_signal_without_a_measured_cost_is_excluded_with_its_reason(
    data: Path, override: dict[str, Any], reason: str,
) -> None:
    offset = activity_offset(data)
    _append(data, [_fill(T2, C1, 600)])
    result = capture(data, offset, "run1", get=_venue(**override).get, now_fn=lambda: NOW)
    assert result["captured"] == 0 and result["excluded"] == {reason: 1}
    (row,) = _signals(data)
    assert row["status"] == "excluded" and row["exclusion_reasons"] == [reason]
    assert "clip" not in row


def test_a_failed_gamma_lookup_leaves_the_clob_fee_unchecked(data: Path) -> None:
    offset = activity_offset(data)
    _append(data, [_fill(T2, C1, 600)])
    venue = _venue(**{"/markets": httpx.ConnectError("down")})
    assert capture(data, offset, "r", get=venue.get, now_fn=lambda: NOW)["captured"] == 1
    assert _signals(data)[0]["fee"]["gamma_fees_enabled"] is None


def test_signals_past_the_time_cap_are_recorded_as_excluded(data: Path) -> None:
    offset = activity_offset(data)
    _append(data, [_fill(T2, C1, 600), _fill(COMPARISON, C2, 300, index=1)])
    ticks = iter([0.0, 0.0, 500.0, 500.0, 500.0])
    result = capture(data, offset, "r", get=_venue().get, now_fn=lambda: NOW,
                     monotonic=lambda: next(ticks), max_seconds=120)
    assert result["captured"] == 1 and result["excluded"] == {"capture time cap reached": 1}


def test_no_member_list_or_no_new_rows_means_no_requests(tmp_path: Path, data: Path) -> None:
    assert capture(tmp_path / "none", 0, "r")["status"] == "skipped"
    offset = activity_offset(data)
    _append(data, [_fill(OTHER, C1, 60)])
    venue = _venue()
    assert capture(data, offset, "r", get=venue.get, now_fn=lambda: NOW)["signals"] == 0
    assert venue.calls == [] and not (data / "cohort-v1" / "signals.jsonl").exists()
    shrunk = capture(data, activity_offset(data) + 1, "r", get=venue.get, now_fn=lambda: NOW)
    assert shrunk == {"status": "skipped", "reason": "activity file shrank during collection"}


def test_an_offset_inside_a_row_skips_the_partial_line(data: Path) -> None:
    _append(data, [_fill(T2, C1, 60)])
    _append(data, [_fill(T2, C2, 60)])
    rows = appended_rows(data, 5)  # inside the first row
    assert [row["condition_id"] for row in rows] == [C2]
    with (data / "polymarket_activity.jsonl").open("a") as handle:
        handle.write('{"proxy_wallet": "0x')  # a torn last row is not read
    assert [row["condition_id"] for row in appended_rows(data, 0)] == [C1, C2]


def test_a_torn_signal_row_is_cut_before_the_next_append(data: Path) -> None:
    (data / "cohort-v1" / "signals.jsonl").write_text('{"signal_id": "torn')
    offset = activity_offset(data)
    _append(data, [_fill(T2, C1, 600)])
    capture(data, offset, "r", get=_venue().get, now_fn=lambda: NOW)
    assert [row["status"] for row in _signals(data)] == ["captured"]


# ── In the pilot ─────────────────────────────────────────────────────────────

def test_collection_captures_its_own_new_rows_before_compaction(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import runner

    _append(data, [_fill(T2, C4, 60)])  # already there before this run

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"windows_succeeded": ["month"], "wallets_with_errors": 0}

    def fake_pipeline(**kwargs: Any) -> Result:
        _append(data, [_fill(T2, C1, 600)])
        return Result()

    seen: dict[str, Any] = {}
    real = cohort_capture.capture

    def fake_capture(data_dir: Path, offset: int, run_id: str) -> dict[str, Any]:
        seen["offset"] = offset
        return real(data_dir, offset, run_id, get=_venue().get, now_fn=lambda: NOW)

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(cohort_capture, "capture", fake_capture)
    monkeypatch.setattr(pilot, "ACTIVITY_COMPACT_MIN_BYTES", 1)  # compaction follows
    result = pilot._execute_stage("collect", data, pilot.PilotConfig(), "run7")
    assert result["cohort_v1_capture"]["captured"] == 1
    assert result["activity_compaction"]["segments"] == 1
    assert [row["signal_id"] for row in _signals(data)] == [f"run7:{T2}:{C1}:0"]

    def broken(data_dir: Path, offset: int, run_id: str) -> dict[str, Any]:
        raise KeyError("bug")

    monkeypatch.setattr(cohort_capture, "capture", broken)
    result = pilot._execute_stage("collect", data, pilot.PilotConfig(), "run8")
    assert result["status"] == "succeeded"  # collection itself is unaffected
    assert result["cohort_v1_capture"] == {"status": "failed", "error_type": "KeyError"}


def test_a_signal_records_every_group_its_wallet_is_in(data: Path) -> None:
    # A CLV-only ablation wallet can also be a comparison wallet (both come from T3).
    roster = json.loads((data / "cohort-v1" / "members.json").read_text())
    roster["clv_only"] = [COMPARISON]
    (data / "cohort-v1" / "members.json").write_text(json.dumps(roster))
    offset = activity_offset(data)
    _append(data, [_fill(COMPARISON, C2, 300, index=1)])
    capture(data, offset, "r", get=_venue().get, now_fn=lambda: NOW)
    assert _signals(data)[0]["groups"] == ["comparison", "clv_only"]
