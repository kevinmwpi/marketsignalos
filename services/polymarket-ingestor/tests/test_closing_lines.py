from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.closing_lines import (
    OBSERVATIONS_FILE,
    RECEIPTS_FILE,
    backfill,
    fetch_window,
    final_conditions,
    parse_close,
    parse_token_ids,
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
