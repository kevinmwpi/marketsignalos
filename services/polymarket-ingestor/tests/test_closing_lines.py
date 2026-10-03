from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.closing_lines import (
    OBSERVATIONS_FILE,
    RECEIPTS_FILE,
    RETRY_AFTER,
    STORE_DIR,
    activity_condition_ids,
    backfill,
    fetch_window,
    final_conditions,
    latest_receipts,
    load_closing_lines,
    parse_close,
    parse_token_ids,
    run_pending,
    select_pending,
)

CLOSE = 1_735_689_600  # 2025-01-01T00:00:00Z
CLOSE_ISO = "2025-01-01T00:00:00Z"


class FakeClob:
    """Gamma + CLOB stand-in. ``history`` maps token id to (t, p) points or an error."""

    def __init__(self, markets: dict[str, dict[str, Any]],
                 history: dict[str, list[tuple[int, float]] | Exception]) -> None:
        self.markets = markets
        self.history = history
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, params: dict[str, Any]) -> Any:
        self.calls.append((url, dict(params)))
        if url.endswith("/markets"):
            return [self.markets[c] for c in params["condition_ids"] if c in self.markets]
        result = self.history[params["market"]]
        if isinstance(result, Exception):
            raise result
        return {"history": [{"t": t, "p": p} for t, p in result]}


def _market(cid: str, token: str, closed_time: str | None = CLOSE_ISO) -> dict[str, Any]:
    row: dict[str, Any] = {"conditionId": cid, "clobTokenIds": json.dumps([token, token + "-no"]),
                           "endDate": "2024-12-31T00:00:00Z"}
    if closed_time:
        row["closedTime"] = closed_time
    return row


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_parsers_handle_gamma_shapes() -> None:
    assert parse_token_ids('["1", "2"]') == ["1", "2"]
    assert parse_token_ids(["1"]) == ["1"]
    assert parse_token_ids("nope") == []
    assert parse_close({"closedTime": "2025-01-01 00:00:00+00", "endDate": "2024-12-31"}) == (
        CLOSE, "closedTime")
    assert parse_close({"endDate": "2024-12-31T00:00:00Z"})[1] == "endDate"
    assert parse_close({}) == (None, "")


def test_window_is_explicit_and_never_uses_interval() -> None:
    fake = FakeClob({}, {"tok": [(CLOSE - 7200, 0.4), (CLOSE - 3600, 0.6), (CLOSE + 60, 1.0)]})
    points = fetch_window("tok", CLOSE, fake, window_hours=48)
    _, params = fake.calls[0]
    # interval=max caps resolved markets at 12 h; an explicit window returns 1 h.
    assert "interval" not in params
    assert params == {"market": "tok", "startTs": CLOSE - 48 * 3600, "endTs": CLOSE,
                      "fidelity": 60}
    assert points == [(CLOSE - 7200, 0.4), (CLOSE - 3600, 0.6)]  # post-close point dropped


def test_backfill_writes_bitemporal_rows_and_a_receipt_per_market(tmp_path: Path) -> None:
    fake = FakeClob(
        {"0xa": _market("0xa", "ta"), "0xb": _market("0xb", "tb")},
        {"ta": [(CLOSE - 5400, 0.3), (CLOSE - 1800, 0.35)], "tb": []},
    )
    summary = backfill(["0xA", "0xb", "0xb"], tmp_path, get=fake)

    assert summary.requested == 2  # deduplicated, case-insensitive
    assert summary.by_status == {"ok": 1, "no_history": 1}
    assert summary.observations_written == 2
    observations = _rows(tmp_path / OBSERVATIONS_FILE)
    assert [o["event_time"] for o in observations] == [
        "2024-12-31T22:30:00Z", "2024-12-31T23:30:00Z"]
    assert all(o["observed_time"] != o["event_time"] for o in observations)
    assert {o["token_id"] for o in observations} == {"ta"}  # the YES token only
    assert observations[0]["source"] == "clob:/prices-history"
    receipts = {r["condition_id"]: r for r in _rows(tmp_path / RECEIPTS_FILE)}
    assert receipts["0xa"]["status"] == "ok"
    assert receipts["0xa"]["hours_last_point_before_close"] == 0.5
    assert receipts["0xa"]["close_field"] == "closedTime"
    assert receipts["0xb"]["status"] == "no_history"  # pre-order-book markets land here


def test_final_markets_are_never_fetched_again(tmp_path: Path) -> None:
    fake = FakeClob({"0xa": _market("0xa", "ta"), "0xb": _market("0xb", "tb")},
                    {"ta": [(CLOSE - 60, 0.5)], "tb": []})
    backfill(["0xa", "0xb"], tmp_path, get=fake)
    before = (tmp_path / OBSERVATIONS_FILE).read_bytes()

    again = FakeClob({}, {})
    summary = backfill(["0xa", "0xb"], tmp_path, get=again)
    assert summary.skipped_final == 2
    assert again.calls == []
    assert (tmp_path / OBSERVATIONS_FILE).read_bytes() == before


def test_open_markets_and_http_errors_are_retried_next_run(tmp_path: Path) -> None:
    first = FakeClob(
        {"0xb": _market("0xb", "tb")},  # 0xa is not closed yet, so Gamma omits it
        {"tb": httpx.ConnectError("reset")},
    )
    summary = backfill(["0xa", "0xb"], tmp_path, get=first)
    assert summary.by_status == {"not_closed": 1, "http_error": 1}
    assert final_conditions(tmp_path) == set()

    second = FakeClob({"0xa": _market("0xa", "ta"), "0xb": _market("0xb", "tb")},
                      {"ta": [(CLOSE - 60, 0.7)], "tb": [(CLOSE - 120, 0.2)]})
    assert backfill(["0xa", "0xb"], tmp_path, get=second).by_status == {"ok": 2}
    assert final_conditions(tmp_path) == {"0xa", "0xb"}


def test_missing_fields_are_recorded_not_guessed(tmp_path: Path) -> None:
    no_token = {"conditionId": "0xa", "closedTime": CLOSE_ISO}
    no_close = {"conditionId": "0xb", "clobTokenIds": '["tb"]'}
    summary = backfill(["0xa", "0xb"], tmp_path,
                       get=FakeClob({"0xa": no_token, "0xb": no_close}, {}))
    assert summary.by_status == {"no_token": 1, "no_close_time": 1}


def test_files_are_append_only_and_tolerate_only_a_torn_last_line(tmp_path: Path) -> None:
    fake = FakeClob({"0xa": _market("0xa", "ta")}, {"ta": [(CLOSE - 60, 0.5)]})
    backfill(["0xa"], tmp_path, get=fake)
    receipts = tmp_path / RECEIPTS_FILE
    receipts.write_text(receipts.read_text() + '{"condition_id": "0xz", "sta')  # crash mid-append
    assert final_conditions(tmp_path) == {"0xa"}

    receipts.write_text("not json\n" + receipts.read_text())
    with pytest.raises(ValueError, match="invalid JSON"):
        final_conditions(tmp_path)


# ── What scoring reads, and how the worker chooses what to fetch ─────────────

def _append(path: Path, *rows: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _observation(cid: str, event: str, observed: str, price: float,
                 outcome_index: int = 0) -> dict[str, Any]:
    return {"condition_id": cid, "token_id": "t", "outcome_index": outcome_index,
            "event_time": event, "observed_time": observed, "price": price,
            "fidelity_minutes": 60, "source": "clob:/prices-history"}


def test_scoring_loads_the_latest_pre_close_yes_point_and_the_no_history_markets(
    tmp_path: Path,
) -> None:
    _append(tmp_path / OBSERVATIONS_FILE,
            _observation("0xA", "2024-12-31T22:00:00Z", "2026-09-29T00:00:00Z", 0.4),
            _observation("0xa", "2024-12-31T23:00:00Z", "2026-09-29T00:00:00Z", 0.6),
            _observation("0xa", "2024-12-31T23:30:00Z", "2026-09-29T00:00:00Z", 0.9, 1),
            _observation("0xb", "2024-12-31T23:00:00Z", "2026-09-29T00:00:00Z", 0.0))
    _append(tmp_path / RECEIPTS_FILE,
            {"condition_id": "0xc", "status": "no_history", "observed_time": "2026-09-29T00:00:00Z"},
            {"condition_id": "0xd", "status": "no_history", "observed_time": "2026-09-29T00:00:00Z"},
            {"condition_id": "0xd", "status": "http_error", "observed_time": "2026-09-30T00:00:00Z"})
    closing = load_closing_lines(tmp_path)
    assert closing.points == {"0xa": (datetime(2024, 12, 31, 23, tzinfo=UTC), 0.6)}
    assert closing.no_history == {"0xc"}  # the latest receipt decides
    assert load_closing_lines(tmp_path / "missing").points == {}


def test_a_frozen_snapshot_never_sees_prices_backfilled_after_it(tmp_path: Path) -> None:
    _append(tmp_path / OBSERVATIONS_FILE,
            _observation("0xa", "2024-12-31T23:00:00Z", "2026-09-29T00:00:00Z", 0.6))
    _append(tmp_path / RECEIPTS_FILE,
            {"condition_id": "0xb", "status": "no_history", "observed_time": "2026-09-29T00:00:00Z"})
    frozen = load_closing_lines(tmp_path, observed_before=datetime(2026, 6, 12, tzinfo=UTC))
    assert (frozen.points, frozen.no_history) == ({}, frozenset())
    assert load_closing_lines(tmp_path, observed_before=datetime(2026, 9, 29, tzinfo=UTC)).points


def test_activity_conditions_are_traded_markets_in_first_seen_order(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "TRADE", "condition_id": "0xB"}),
        "not json",
        json.dumps({"type": "REDEEM", "condition_id": "0xz"}),
        json.dumps({"type": "TRADE", "condition_id": "0xa"}),
        json.dumps({"type": "TRADE", "condition_id": "0xb"}),
    ]) + "\n")
    assert activity_condition_ids(path) == ["0xb", "0xa"]
    assert activity_condition_ids(tmp_path / "missing.jsonl") == []


def test_selection_skips_final_markets_and_waits_before_retrying_the_rest() -> None:
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    receipts = {
        "0xok": ("ok", now - timedelta(days=9)),
        "0xgone": ("no_history", now - timedelta(days=9)),
        "0xopen_recent": ("not_closed", now - timedelta(hours=1)),
        "0xopen_old": ("not_closed", now - RETRY_AFTER),
        "0xerror_older": ("http_error", now - RETRY_AFTER - timedelta(hours=5)),
    }
    ids = ["0xok", "0xgone", "0xopen_recent", "0xopen_old", "0xerror_older", "0xnew1", "0xnew2"]
    assert select_pending(ids, receipts, now=now, limit=10) == [
        "0xnew1", "0xnew2", "0xerror_older", "0xopen_old"]  # never tried, then stalest retry
    assert select_pending(ids, receipts, now=now, limit=3) == ["0xnew1", "0xnew2", "0xerror_older"]
    # Priority markets jump the queue in the same order; final ones stay skipped.
    priority = {"0xopen_old", "0xnew2", "0xok", "0xelsewhere"}
    assert select_pending(ids, receipts, now=now, limit=10, priority=priority) == [
        "0xnew2", "0xopen_old", "0xnew1", "0xerror_older"]


class Tick:
    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_backfill_starts_no_market_after_its_deadline(tmp_path: Path) -> None:
    fake = FakeClob({c: _market(c, "t" + c) for c in ("0xa", "0xb", "0xc")},
                    {"t0xa": [(CLOSE - 60, 0.5)], "t0xb": [(CLOSE - 60, 0.5)],
                     "t0xc": [(CLOSE - 60, 0.5)]})
    summary = backfill(["0xa", "0xb", "0xc"], tmp_path, get=fake, deadline=2.5, clock=Tick(1.0))
    assert summary.stopped_early
    assert summary.by_status == {"ok": 2}
    assert set(latest_receipts(tmp_path)) == {"0xa", "0xb"}  # 0xc waits for the next run


def test_worker_pass_fetches_what_wallets_traded_and_is_bounded(tmp_path: Path) -> None:
    (tmp_path / "polymarket_activity.jsonl").write_text("".join(
        json.dumps({"type": "TRADE", "condition_id": c}) + "\n" for c in ("0xa", "0xb", "0xc")))
    fake = FakeClob({"0xa": _market("0xa", "ta"), "0xb": _market("0xb", "tb")},
                    {"ta": [(CLOSE - 60, 0.5)], "tb": []})
    now = datetime(2026, 9, 30, tzinfo=UTC)
    first = run_pending(tmp_path, limit=2, max_seconds=60, get=fake, now=now)
    assert first["status"] == "succeeded"
    assert (first["conditions_in_activity"], first["selected"]) == (3, 2)
    assert first["summary"]["by_status"] == {"ok": 1, "no_history": 1}
    assert (tmp_path / STORE_DIR / OBSERVATIONS_FILE).exists()

    second = run_pending(tmp_path, limit=2, max_seconds=60, get=fake, now=now)
    assert second["selected"] == 1  # only 0xc is left; it is still open
    assert second["summary"]["by_status"] == {"not_closed": 1}
    assert run_pending(tmp_path, limit=2, max_seconds=60, get=fake, now=now)["selected"] == 0


def test_worker_pass_fetches_priority_markets_first(tmp_path: Path) -> None:
    (tmp_path / "polymarket_activity.jsonl").write_text("".join(
        json.dumps({"type": "TRADE", "condition_id": c}) + "\n" for c in ("0xa", "0xb", "0xc")))
    fake = FakeClob({c: _market(c, "t" + c) for c in ("0xa", "0xb", "0xc")},
                    {"t0xa": [], "t0xb": [], "t0xc": [(CLOSE - 60, 0.5)]})
    now = datetime(2026, 9, 30, tzinfo=UTC)
    priority = {"0xc", "0xuntraded"}
    first = run_pending(tmp_path, limit=1, max_seconds=60, get=fake, now=now, priority=priority)
    assert (first["priority_open"], first["priority_selected"]) == (1, 1)
    assert set(latest_receipts(tmp_path / STORE_DIR)) == {"0xc"}
    again = run_pending(tmp_path, limit=1, max_seconds=60, get=fake, now=now, priority=priority)
    assert (again["priority_open"], again["priority_selected"]) == (0, 0)


def test_a_failed_market_lookup_is_partial_and_keeps_details_out_of_the_result(
    tmp_path: Path,
) -> None:
    (tmp_path / "polymarket_activity.jsonl").write_text(
        json.dumps({"type": "TRADE", "condition_id": "0xa"}) + "\n")

    def broken(url: str, params: dict[str, Any]) -> Any:
        raise httpx.ConnectError("https://secret.example/?token=abc")

    result = run_pending(tmp_path, limit=5, max_seconds=60, get=broken)
    assert result["status"] == "partial"
    assert result["error_type"] == "ConnectError"
    assert "secret" not in json.dumps(result)
