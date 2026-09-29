import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe_price_history as probe

CLOSE = 1_700_000_000


def test_token_ids_accept_json_strings_and_lists() -> None:
    assert probe.parse_token_ids('["111", "222"]') == ["111", "222"]
    assert probe.parse_token_ids(["111", 222]) == ["111", "222"]
    assert probe.parse_token_ids("not json") == []
    assert probe.parse_token_ids(None) == []


def test_close_time_prefers_actual_close_over_schedule() -> None:
    market = {"closedTime": "2024-11-06 12:00:00+00", "endDate": "2024-11-05T00:00:00Z"}
    ts, field = probe.close_timestamp(market)
    assert field == "closedTime"
    assert ts == probe.parse_iso("2024-11-06T12:00:00+00:00")
    assert probe.close_timestamp({"endDate": "2024-11-05"})[1] == "endDate"
    assert probe.close_timestamp({}) == (None, "")


def test_series_accepts_history_wrapper_and_skips_bad_rows() -> None:
    payload = {"history": [{"t": 3, "p": 0.3}, {"t": 1, "p": "0.1"}, {"t": "x"}, {"p": 1}]}
    assert probe.series_points(payload) == [(1, 0.1), (3, 0.3)]
    assert probe.series_points({"error": "x"}) == []


def test_summary_measures_the_gap_to_close_ignoring_later_points() -> None:
    points = [(CLOSE - 43_200, 0.40), (CLOSE - 3_600, 0.55), (CLOSE + 3_600, 1.0)]
    s = probe.summarize_series(points, CLOSE)
    assert s["points"] == 3
    assert s["last_before_close_price"] == 0.55
    assert s["hours_last_point_before_close"] == 1.0  # the post-close point is not the line
    assert s["median_spacing_minutes"] == 390.0
    assert probe.summarize_series([], CLOSE) == {"points": 0}


def test_aggregate_reports_share_with_data_per_group_and_request() -> None:
    def market(group: str, points: int, gap: float | None) -> dict[str, Any]:
        summary: dict[str, Any] = {"points": points}
        if points:
            summary.update(hours_last_point_before_close=gap, median_spacing_minutes=720.0)
        return {"group": group, "requests": {"max@720m": summary}}

    rows = probe.aggregate([
        market("resolved-2022", 10, 6.0), market("resolved-2022", 0, None),
        market("resolved-2022", 8, 10.0), market("open", 5, 2.0),
    ])
    by_group = {r["group"]: r for r in rows}
    assert by_group["resolved-2022"]["with_data"] == 2
    assert by_group["resolved-2022"]["share_with_data"] == 0.667
    assert by_group["resolved-2022"]["median_hours_last_point_before_close"] == 8.0
    assert by_group["open"]["markets"] == 1


def test_probe_market_records_every_request_even_on_errors() -> None:
    calls: list[dict[str, Any]] = []

    def get(url: str, params: dict[str, Any]) -> tuple[int | None, Any, str | None]:
        calls.append(params)
        if params.get("fidelity") == 1:
            return None, None, "ConnectError: boom"
        return 200, {"history": [{"t": CLOSE - 7200, "p": 0.5}]}, None

    market = {"token_id": "123", "close_ts": CLOSE, "group": "resolved-2024", "requests": {}}
    probe.probe_market(get, market)
    assert set(market["requests"]) == {
        "max@1m", "max@60m", "max@360m", "max@720m", "max@1440m",
        "window14d@60m", "window14d@720m",
    }
    assert market["requests"]["max@1m"]["error"] == "ConnectError: boom"
    assert market["requests"]["window14d@720m"]["hours_last_point_before_close"] == 2.0
    assert len(calls) == 7
