from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.closing_lines import ACTIVITY_FILE
from marketsignalos_polymarket.entry_prices import (
    ARCHIVE_FILE,
    CHUNK_SECONDS,
    HORIZON_SECONDS,
    OBSERVATIONS_FILE,
    RECEIPTS_FILE,
    STORE_DIR,
    MarketLookup,
    backfill,
    chunk_start,
    close_times,
    compact_observations,
    latest_receipts,
    load_entry_prices,
    lookup_markets,
    needed_chunks,
    price_after,
    run_pending,
    select_pending,
)

C = CHUNK_SECONDS
W = 2870 * C  # 2025-01-02T00:00:00Z, a chunk boundary
HOUR = 3600
CLOSED_AT = "2025-01-20 06:30:00+00"  # Gamma's closedTime format


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


class FakeApi:
    """Gamma + CLOB stand-in. Gamma honours the ``closed`` filter, as the real one does.

    ``markets`` maps condition id to (YES token, closed); ``history`` maps token id to
    (t, p) points or an error. Every market has a scheduled end; closed ones also have
    a ``closedTime``.
    """

    def __init__(self, markets: dict[str, tuple[str, bool]],
                 history: dict[str, list[tuple[int, float]] | Exception]) -> None:
        self.markets = markets
        self.history = history
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, params: dict[str, Any]) -> Any:
        self.calls.append((url, dict(params)))
        if url.endswith("/markets"):
            closed = params["closed"] == "true"
            return [{"conditionId": cid, "clobTokenIds": json.dumps([token, token + "-no"]),
                     "endDate": "2025-03-01T00:00:00Z",
                     **({"closedTime": CLOSED_AT} if is_closed else {})}
                    for cid in params["condition_ids"]
                    for token, is_closed in [self.markets.get(cid, ("", not closed))]
                    if token and is_closed == closed]
        result = self.history[params["market"]]
        if isinstance(result, Exception):
            raise result
        return {"history": [{"t": t, "p": p} for t, p in result]}

    def price_requests(self) -> list[dict[str, Any]]:
        return [params for url, params in self.calls if url.endswith("/prices-history")]


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_activity(data_dir: Path, rows: list[Any]) -> None:
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    (data_dir / ACTIVITY_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _buy(cid: str, ts: object, **extra: Any) -> dict[str, Any]:
    return {"type": "TRADE", "side": "BUY", "condition_id": cid, "timestamp": ts, **extra}


def _receipt(cid: str, start: int, status: str, observed: datetime) -> dict[str, Any]:
    return {"condition_id": cid, "chunk_start": _at(start).isoformat(),
            "chunk_end": _at(start + C).isoformat(), "status": status,
            "observed_time": observed.isoformat()}


def _write_receipts(store: Path, rows: list[dict[str, Any]]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    with (store / RECEIPTS_FILE).open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


# ── Which chunks ─────────────────────────────────────────────────────────────

def test_needed_chunks_cover_the_window_after_each_buy(tmp_path: Path) -> None:
    assert HORIZON_SECONDS == 6 * HOUR  # the longest candidate horizon since 2026-10-03
    _write_activity(tmp_path, [
        _buy("0xA", W + C - 3 * HOUR),  # the window runs into the next chunk
        _buy("0xb", W + C - HORIZON_SECONDS),  # it ends exactly on the next chunk's start
        _buy("0xh", W),  # inside one chunk
        {**_buy("0xc", W), "side": "SELL"},
        {**_buy("0xd", W), "type": "REDEEM"},
        _buy("0xe", 0), _buy("0xf", True), _buy("", W), _buy("0xg", "1735776000"),
        "not json", "[1, 2]",
    ])
    assert needed_chunks(tmp_path / ACTIVITY_FILE) == {
        ("0xa", W), ("0xa", W + C), ("0xb", W), ("0xb", W + C), ("0xh", W)}
    assert needed_chunks(tmp_path / ACTIVITY_FILE, horizon_seconds=HOUR) == {
        ("0xa", W), ("0xb", W), ("0xh", W)}
    assert needed_chunks(tmp_path / "missing.jsonl") == set()
    assert chunk_start(W + C - 1) == W
    assert HORIZON_SECONDS <= CHUNK_SECONDS  # a buy never reaches more than two chunks


def test_select_pending_waits_for_ended_chunks_and_skips_final_ones() -> None:
    now = _at(W + 3 * C + 1800)
    needed = {("0xa", W), ("0xa", W + 2 * C), ("0xb", W), ("0xc", W), ("0xd", W),
              ("0xe", W + C)}
    receipts = {
        ("0xb", W): ("ok", now - timedelta(days=30)),
        ("0xc", W): ("http_error", now - timedelta(hours=1)),
        ("0xd", W): ("no_token", now - timedelta(hours=25)),
    }
    # (0xa, W + 2C) ends at W + 3C, less than an hour before now: it could still change.
    assert select_pending(needed, receipts, now=now, limit=10) == [
        ("0xe", W + C), ("0xa", W), ("0xd", W)]
    assert select_pending(needed, receipts, now=now, limit=2) == [("0xe", W + C), ("0xa", W)]
    assert select_pending(needed, receipts, now=now, limit=0) == []
    # Priority chunks jump the queue, retries included, keeping the order among them.
    priority = {("0xa", W), ("0xd", W), ("0xb", W), ("0xa", W + 2 * C), ("0xz", W)}
    assert select_pending(needed, receipts, now=now, limit=10, priority=priority) == [
        ("0xa", W), ("0xd", W), ("0xe", W + C)]
    assert select_pending(needed, receipts, now=now, limit=1, priority=priority) == [
        ("0xa", W)]
    receipts[("0xa", W)] = ("empty", now)  # empty is final: no order-book history
    assert ("0xa", W) not in select_pending(needed, receipts, now=now, limit=10)


def test_latest_receipts_apply_the_observed_before_cutoff(tmp_path: Path) -> None:
    first = _at(W + 2 * C)
    _write_receipts(tmp_path, [_receipt("0xA", W, "http_error", first),
                               _receipt("0xa", W, "ok", first + timedelta(days=1))])
    assert latest_receipts(tmp_path) == {("0xa", W): ("ok", first + timedelta(days=1))}
    assert latest_receipts(tmp_path, observed_before=first) == {
        ("0xa", W): ("http_error", first)}


# ── Fetching ─────────────────────────────────────────────────────────────────

def test_token_lookup_asks_for_open_markets_then_closed_ones() -> None:
    api = FakeApi({"0xa": ("tok-a", False), "0xb": ("tok-b", True)}, {})
    # Only an actual close counts, never the scheduled end date.
    assert lookup_markets(["0xa", "0xb", "0xc"], api) == {
        "0xa": MarketLookup("tok-a", ""), "0xb": MarketLookup("tok-b", "2025-01-20T06:30:00Z")}
    asked = [(params["closed"], params["condition_ids"]) for _, params in api.calls]
    assert asked == [("false", ["0xa", "0xb", "0xc"]), ("true", ["0xb", "0xc"])]


def test_backfill_records_every_outcome_and_keeps_points_inside_the_chunk(
    tmp_path: Path,
) -> None:
    api = FakeApi(
        {"0xa": ("tok-a", True), "0xb": ("tok-b", True), "0xd": ("tok-d", False)},
        {"tok-a": [(W - HOUR, 0.1), (W, 0.4), (W + HOUR, 0.45), (W + C, 0.9)],
         "tok-b": [],
         "tok-d": httpx.ConnectError("boom")},
    )
    store = tmp_path / STORE_DIR
    summary = backfill([("0xa", W), ("0xb", W), ("0xc", W), ("0xd", W)], store, get=api)

    assert summary.by_status == {"ok": 1, "empty": 1, "no_token": 1, "http_error": 1}
    assert summary.observations_written == 2 and not summary.stopped_early
    receipts = {row["condition_id"]: row for row in _rows(store / RECEIPTS_FILE)}
    assert receipts["0xa"]["points"] == 2 and receipts["0xa"]["token_id"] == "tok-a"
    assert receipts["0xa"]["market_closed_time"] == "2025-01-20T06:30:00Z"
    assert receipts["0xd"]["market_closed_time"] == ""  # still open
    assert receipts["0xa"]["chunk_end"] == _at(W + C).isoformat().replace("+00:00", "Z")
    assert receipts["0xd"]["error"] == "boom"
    observations = _rows(store / OBSERVATIONS_FILE)
    assert [(o["event_time"], o["price"], o["outcome_index"]) for o in observations] == [
        ("2025-01-02T00:00:00Z", 0.4, 0), ("2025-01-02T01:00:00Z", 0.45, 0)]
    assert all(o["observed_time"] == receipts["0xa"]["observed_time"] for o in observations)
    for params in api.price_requests():
        assert params["startTs"] == W and params["endTs"] == W + C
        assert params["fidelity"] == 60 and "interval" not in params


def test_backfill_starts_no_chunk_after_the_deadline(tmp_path: Path) -> None:
    api = FakeApi({"0xa": ("tok-a", True), "0xb": ("tok-b", True)},
                  {"tok-a": [(W, 0.5)], "tok-b": [(W, 0.5)]})
    ticks = iter([0.0, 11.0])
    summary = backfill([("0xa", W), ("0xb", W)], tmp_path, get=api, deadline=10.0,
                       clock=lambda: next(ticks))
    assert summary.stopped_early and summary.by_status == {"ok": 1}
    assert [row["condition_id"] for row in _rows(tmp_path / RECEIPTS_FILE)] == ["0xa"]


# ── The pilot stage ──────────────────────────────────────────────────────────

def test_run_pending_fetches_each_ended_chunk_once(tmp_path: Path) -> None:
    _write_activity(tmp_path, [_buy("0xa", W + 86400), _buy("0xa", W + C - 2 * HOUR)])
    api = FakeApi({"0xa": ("tok-a", True)},
                  {"tok-a": [(W + 2 * 86400 - HOUR, 0.3), (W + C, 0.35)]})
    now = _at(W + 2 * C + 2 * HOUR)

    first = run_pending(tmp_path, limit=10, max_seconds=60, get=api, now=now)
    assert first["status"] == "succeeded"
    assert first["chunks_needed"] == 2 and first["selected"] == 2
    assert first["summary"]["by_status"] == {"ok": 2}
    # The pass compacted what it fetched: only the archive is left.
    assert first["compacted_bytes"] > 0
    assert not (tmp_path / STORE_DIR / OBSERVATIONS_FILE).exists()
    assert (tmp_path / STORE_DIR / ARCHIVE_FILE).exists()
    requests = len(api.calls)

    again = run_pending(tmp_path, limit=10, max_seconds=60, get=api, now=now)
    assert again["status"] == "succeeded" and again["selected"] == 0
    assert len(api.calls) == requests  # final chunks are never fetched again
    series = load_entry_prices(tmp_path / STORE_DIR)["0xa"]
    assert price_after(series, W + 86400, 86400) == 0.3


def test_run_pending_is_partial_when_a_chunk_fails(tmp_path: Path) -> None:
    _write_activity(tmp_path, [_buy("0xa", W + C - HOUR)])
    api = FakeApi({"0xa": ("tok-a", True)}, {"tok-a": httpx.ReadTimeout("slow")})
    result = run_pending(tmp_path, limit=10, max_seconds=60, get=api, now=_at(W + 3 * C))
    assert result["status"] == "partial"
    assert result["summary"]["by_status"] == {"http_error": 2}


def test_run_pending_is_partial_without_details_when_the_lookup_fails(tmp_path: Path) -> None:
    _write_activity(tmp_path, [_buy("0xa", W + C - HOUR)])

    def broken(url: str, params: dict[str, Any]) -> Any:
        return {"error": "https://gamma.example/?token=secret"}

    result = run_pending(tmp_path, limit=10, max_seconds=60, get=broken, now=_at(W + 3 * C))
    assert result == {"status": "partial", "chunks_needed": 2, "selected": 2,
                      "priority_open": 0, "priority_selected": 0,
                      "error_type": "ValueError", "compacted_bytes": 0}
    assert not (tmp_path / STORE_DIR / RECEIPTS_FILE).exists()


def test_run_pending_fetches_priority_chunks_first_and_counts_them(tmp_path: Path) -> None:
    _write_activity(tmp_path, [_buy("0xold", W + C - HOUR), _buy("0xnew", W + C)])
    api = FakeApi({"0xold": ("tok-o", True), "0xnew": ("tok-n", True)},
                  {"tok-o": [(W, 0.5)], "tok-n": [(W + C, 0.5)]})
    now = _at(W + 3 * C + 2 * HOUR)
    priority = {("0xold", W), ("0xold", W + C), ("0xmissing", W)}

    first = run_pending(tmp_path, limit=1, max_seconds=60, get=api, now=now,
                        priority=priority)
    assert (first["priority_open"], first["priority_selected"]) == (2, 1)
    assert [row["condition_id"] for row in _rows(tmp_path / STORE_DIR / RECEIPTS_FILE)] == [
        "0xold"]  # newest-first alone would have picked 0xnew
    second = run_pending(tmp_path, limit=10, max_seconds=60, get=api, now=now,
                         priority=priority)
    assert (second["priority_open"], second["priority_selected"]) == (1, 1)
    assert run_pending(tmp_path, limit=10, max_seconds=60, get=api, now=now,
                       priority=priority)["priority_open"] == 0


def test_close_times_come_from_the_latest_receipt_that_has_one(tmp_path: Path) -> None:
    observed = _at(W + 2 * C)
    _write_receipts(tmp_path, [
        {**_receipt("0xA", W, "ok", observed), "market_closed_time": "2025-01-20T06:30:00Z"},
        _receipt("0xa", W + C, "ok", observed),  # written before close times were kept
        {**_receipt("0xb", W, "ok", observed), "market_closed_time": ""},
    ])
    assert close_times(tmp_path) == {"0xa": int(datetime(2025, 1, 20, 6, 30, tzinfo=UTC)
                                                .timestamp())}


def test_run_pending_with_nothing_to_do_makes_no_requests(tmp_path: Path) -> None:
    def unreachable(url: str, params: dict[str, Any]) -> Any:
        raise AssertionError("no request expected")

    result = run_pending(tmp_path, limit=10, max_seconds=60, get=unreachable, now=_at(W))
    assert result["status"] == "succeeded" and result["chunks_needed"] == 0


# ── Reading ──────────────────────────────────────────────────────────────────

def test_load_entry_prices_respects_the_cutoff_and_the_price_range(tmp_path: Path) -> None:
    fetched = _at(W + 2 * C)
    later = fetched + timedelta(days=1)
    rows = [
        {"condition_id": "0xA", "outcome_index": 0, "event_time": _at(W + HOUR).isoformat(),
         "observed_time": fetched.isoformat(), "price": 0.2},
        {"condition_id": "0xa", "outcome_index": 0, "event_time": _at(W).isoformat(),
         "observed_time": fetched.isoformat(), "price": 0.0},  # a real price, kept
        {"condition_id": "0xa", "outcome_index": 0, "event_time": _at(W + HOUR).isoformat(),
         "observed_time": later.isoformat(), "price": 0.25},  # refetch: latest row wins
        {"condition_id": "0xa", "outcome_index": 1, "event_time": _at(W).isoformat(),
         "observed_time": fetched.isoformat(), "price": 0.9},
        {"condition_id": "0xa", "outcome_index": 0, "event_time": _at(W + 2 * HOUR).isoformat(),
         "observed_time": fetched.isoformat(), "price": 1.2},
    ]
    tmp_path.joinpath(OBSERVATIONS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + '{"torn', encoding="utf-8")

    assert load_entry_prices(tmp_path) == {"0xa": [(W, 0.0), (W + HOUR, 0.25)]}
    assert load_entry_prices(tmp_path, observed_before=fetched) == {
        "0xa": [(W, 0.0), (W + HOUR, 0.2)]}


def test_price_after_takes_the_last_point_near_the_horizon() -> None:
    series = [(W, 0.4), (W + HOUR, 0.5), (W + 5 * HOUR, 0.7)]
    assert price_after(series, W - 1, HOUR + 1) == 0.5  # exactly on a point
    assert price_after(series, W, 2 * HOUR) == 0.5  # between points: the one before
    assert price_after(series, W, 4 * HOUR) is None  # three hours stale: a gap
    assert price_after(series, W, 4 * HOUR, tolerance_seconds=3 * HOUR) == 0.5
    assert price_after(series, W + HOUR, 30 * 60) is None  # nothing after the buy yet
    assert price_after(series, W - 2 * HOUR, HOUR) is None  # before the series starts
    assert price_after(series, W, 30 * HOUR) is None  # the market stopped publishing


# ── Compaction (owner's approval, 2026-10-06) ────────────────────────────────

def _observation(cid: str, ts: int, price: float, observed: datetime) -> dict[str, Any]:
    return {"condition_id": cid, "token_id": "tok", "outcome_index": 0,
            "event_time": _at(ts).isoformat().replace("+00:00", "Z"),
            "observed_time": observed.isoformat().replace("+00:00", "Z"), "price": price,
            "fidelity_minutes": 60, "source": "clob_prices_history"}


def _append_plain(store: Path, rows: list[dict[str, Any]], tail: str = "") -> None:
    store.mkdir(parents=True, exist_ok=True)
    with (store / OBSERVATIONS_FILE).open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in rows) + tail)


def test_compaction_moves_every_row_and_changes_no_read(tmp_path: Path) -> None:
    store = tmp_path / STORE_DIR
    fetched, later = _at(W + 2 * C), _at(W + 3 * C)
    _append_plain(store, [_observation("0xa", W + i * HOUR, 0.4 + i / 100, fetched)
                          for i in range(5)])
    before = load_entry_prices(store)
    as_of = load_entry_prices(store, observed_before=fetched)

    moved = compact_observations(store)
    assert moved > 0 and not (store / OBSERVATIONS_FILE).exists()
    assert load_entry_prices(store) == before
    assert load_entry_prices(store, observed_before=fetched) == as_of

    # A second pass appends a second gzip member; a refetched point still wins.
    _append_plain(store, [_observation("0xa", W + HOUR, 0.6, later),
                          _observation("0xb", W, 0.3, later)])
    compact_observations(store)
    after = load_entry_prices(store)
    assert after["0xa"][1] == (W + HOUR, 0.6) and after["0xb"] == [(W, 0.3)]
    assert load_entry_prices(store, observed_before=fetched) == as_of  # point in time holds
    assert compact_observations(store) == 0  # nothing left to move


def test_compaction_drops_only_a_torn_final_row(tmp_path: Path) -> None:
    store = tmp_path / STORE_DIR
    _append_plain(store, [_observation("0xa", W, 0.4, _at(W + 2 * C))], tail='{"torn')
    size = (store / OBSERVATIONS_FILE).stat().st_size
    assert compact_observations(store) == size - len('{"torn')
    assert load_entry_prices(store) == {"0xa": [(W, 0.4)]}
    # Rows written after the torn one start a clean line in the next member.
    _append_plain(store, [_observation("0xa", W + HOUR, 0.5, _at(W + 2 * C))])
    compact_observations(store)
    assert load_entry_prices(store) == {"0xa": [(W, 0.4), (W + HOUR, 0.5)]}


def test_a_crash_after_the_swap_only_duplicates_rows(tmp_path: Path) -> None:
    store = tmp_path / STORE_DIR
    rows = [_observation("0xa", W + i * HOUR, 0.4, _at(W + 2 * C)) for i in range(3)]
    _append_plain(store, rows)
    plain = (store / OBSERVATIONS_FILE).read_bytes()
    compact_observations(store)
    expected = load_entry_prices(store)
    (store / OBSERVATIONS_FILE).write_bytes(plain)  # the unlink never happened
    _append_plain(store, [_observation("0xa", W + 3 * HOUR, 0.5, _at(W + 2 * C))])
    compact_observations(store)
    assert load_entry_prices(store) == {"0xa": expected["0xa"] + [(W + 3 * HOUR, 0.5)]}


def test_a_failed_compaction_leaves_both_files_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    store = tmp_path / STORE_DIR
    _append_plain(store, [_observation("0xa", W, 0.4, _at(W + 2 * C))])
    compact_observations(store)
    _append_plain(store, [_observation("0xa", W + HOUR, 0.5, _at(W + 2 * C))])
    archive = (store / ARCHIVE_FILE).read_bytes()
    plain = (store / OBSERVATIONS_FILE).read_bytes()

    def no_disk(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", no_disk)
    with pytest.raises(OSError, match="disk full"):
        compact_observations(store)
    assert (store / ARCHIVE_FILE).read_bytes() == archive
    assert (store / OBSERVATIONS_FILE).read_bytes() == plain
    assert {p.name for p in store.iterdir()} == {ARCHIVE_FILE, OBSERVATIONS_FILE}


def test_a_pass_after_a_crash_mid_row_never_joins_the_torn_row_to_new_ones(
    tmp_path: Path,
) -> None:
    # A worker killed mid-append leaves a row without its newline. Appending the
    # next pass's rows straight after it would fuse the two into one invalid line
    # in the middle of the store, and every later read would fail.
    store = tmp_path / STORE_DIR
    _append_plain(store, [_observation("0xa", W, 0.4, _at(W + 2 * C))], tail='{"torn')
    _write_activity(tmp_path, [_buy("0xb", W + HOUR)])
    api = FakeApi({"0xb": ("tok-b", True)}, {"tok-b": [(W + 2 * HOUR, 0.3)]})
    result = run_pending(tmp_path, limit=10, max_seconds=60, get=api,
                         now=_at(W + 2 * C + 2 * HOUR))
    assert result["summary"]["by_status"] == {"ok": 1}
    assert load_entry_prices(store) == {"0xa": [(W, 0.4)], "0xb": [(W + 2 * HOUR, 0.3)]}


def test_a_torn_receipt_is_cut_before_the_next_pass_appends(tmp_path: Path) -> None:
    store = tmp_path / STORE_DIR
    api = FakeApi({"0xa": ("tok-a", True), "0xb": ("tok-b", True)},
                  {"tok-a": [(W, 0.5)], "tok-b": [(W, 0.6)]})
    backfill([("0xa", W)], store, get=api)
    with (store / RECEIPTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"torn')
    backfill([("0xb", W)], store, get=api)
    assert set(latest_receipts(store)) == {("0xa", W), ("0xb", W)}
