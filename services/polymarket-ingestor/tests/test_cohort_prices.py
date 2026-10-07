from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket import cohort_prices
from marketsignalos_polymarket import lean_pilot as pilot
from marketsignalos_polymarket.cohort_prices import load_signal_prices, run_pending

# Relative to the real clock: receipts record when a window was really fetched.
DETECTED = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=10)
T0 = int(DETECTED.timestamp())
READY = DETECTED + timedelta(hours=7)  # 6 h window + 1 h to settle


def _signal(sid: str, *, token: str = "tok", status: str = "captured",
            detected: datetime = DETECTED) -> dict[str, Any]:
    return {"signal_id": sid, "condition_id": "0xc", "outcome_index": 1, "token_id": token,
            "detected_at": detected.isoformat(), "status": status}


@pytest.fixture
def data(tmp_path: Path) -> Path:
    stage = tmp_path / "cohort-v1"
    stage.mkdir()
    rows = [_signal("s1"), _signal("s2", status="excluded"), _signal("s3", token=""),
            _signal("s4", detected=DETECTED + timedelta(hours=2))]
    (stage / "signals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


class History:
    def __init__(self, points: list[tuple[int, float]] | Exception) -> None:
        self.points = points
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any]) -> Any:
        assert url.endswith("/prices-history")
        self.calls.append(params)
        if isinstance(self.points, Exception):
            raise self.points
        return {"history": [{"t": t, "p": p} for t, p in self.points]}


def _receipts(data: Path) -> list[dict[str, Any]]:
    path = data / "cohort-v1" / "price_receipts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_settled_window_is_fetched_once_for_the_token_bought(data: Path) -> None:
    points = [(T0 - 7200, 0.1), (T0 - 300, 0.40), (T0 + 3300, 0.45), (T0 + 3600, 0.46),
              (T0 + 30000, 0.9)]  # the first and last fall outside the window
    history = History(points)
    result = run_pending(data, max_seconds=60, get=history.get, now=READY)
    # s1 and s2 (excluded signals too); s3 has no token; s4's window has not settled.
    assert result["due"] == 2 and result["by_status"] == {"ok": 2}
    assert result["observations_written"] == 6 and result["status"] == "succeeded"
    assert history.calls[0] == {"market": "tok", "startTs": T0 - 3600,
                                "endTs": T0 + 6 * 3600, "fidelity": 5}
    receipt = _receipts(data)[0]
    assert receipt["points"] == 3 and receipt["median_spacing_seconds"] == 1950
    assert load_signal_prices(data)["s1"] == [(T0 - 300, 0.40), (T0 + 3300, 0.45),
                                              (T0 + 3600, 0.46)]
    # Final windows are never fetched again; s4 becomes due once settled.
    assert run_pending(data, max_seconds=60, get=history.get, now=READY)["due"] == 0
    later = READY + timedelta(hours=2)
    assert run_pending(data, max_seconds=60, get=history.get, now=later)["due"] == 1


def test_a_failed_window_is_retried_after_an_hour(data: Path) -> None:
    down = History(httpx.ConnectError("down"))
    result = run_pending(data, max_seconds=60, get=down.get, now=READY)
    assert result["status"] == "partial" and result["by_status"] == {"http_error": 2}
    assert not (data / "cohort-v1" / "price_observations.jsonl").exists() or not (
        data / "cohort-v1" / "price_observations.jsonl").read_text()
    up = History([(T0 + 3600, 0.5)])
    soon = datetime.now(UTC) + timedelta(minutes=30)
    assert run_pending(data, max_seconds=60, get=up.get, now=soon)["due"] == 1  # s4 only
    retry = datetime.now(UTC) + timedelta(hours=2)
    assert run_pending(data, max_seconds=60, get=up.get, now=retry)["by_status"] == {"ok": 2}


def test_an_empty_window_is_final_and_the_pass_stops_at_its_deadline(data: Path) -> None:
    ticks = iter([0.0, 0.0, 100.0])
    result = run_pending(data, max_seconds=60, get=History([]).get, now=READY,
                         clock=lambda: next(ticks))
    assert result["stopped_early"] and result["by_status"] == {"empty": 1}
    assert result["status"] == "partial"


def test_a_torn_receipt_is_cut_before_the_next_append(data: Path) -> None:
    (data / "cohort-v1" / "price_receipts.jsonl").write_text('{"signal_id": "s1", "sta')
    run_pending(data, max_seconds=60, get=History([(T0, 0.5)]).get, now=READY)
    assert [r["signal_id"] for r in _receipts(data)] == ["s1", "s2"]


def test_the_entry_price_stage_fetches_signal_windows_and_survives_their_failure(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import entry_prices

    monkeypatch.setattr(entry_prices, "run_pending",
                        lambda data_dir, **kwargs: {"status": "succeeded", "selected": 0})
    monkeypatch.setattr(cohort_prices, "run_pending",
                        lambda data_dir, **kwargs: {"status": "succeeded", "due": 0})
    result = pilot._execute_stage("entry_prices", data, pilot.PilotConfig(), "r")
    assert result["status"] == "succeeded" and result["cohort_v1_prices"]["due"] == 0

    def broken(data_dir: Path, **kwargs: Any) -> dict[str, Any]:
        raise KeyError("bug")

    monkeypatch.setattr(cohort_prices, "run_pending", broken)
    result = pilot._execute_stage("entry_prices", data, pilot.PilotConfig(), "r")
    assert result["status"] == "succeeded"
    assert result["cohort_v1_prices"] == {"status": "failed", "error_type": "KeyError"}
