from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import lean_pilot as pilot


class Clock:
    def __init__(self, at: datetime | None = None) -> None:
        self.at = at or datetime(2026, 9, 9, 12, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


def state_at(data_dir: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(
        (data_dir / ".lean-pilot" / "state.json").read_text(encoding="utf-8")
    )
    return state


def test_collection_and_scoring_have_independent_durable_cadences(tmp_path: Path) -> None:
    clock = Clock()
    config = pilot.PilotConfig()
    calls: list[str] = []

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        calls.append(stage)
        clock.advance(5)
        return {"status": "succeeded", "rows": 7}

    def cycle(run: str) -> dict[str, Any]:
        return pilot.run_cycle(tmp_path, config, run, execute=execute,
                               now_fn=clock.now, monotonic=clock.monotonic)

    first = cycle("first")
    assert first["status"] == "succeeded"
    assert calls == ["collect", "score"]
    assert state_at(tmp_path)["days"]["2026-09-09"] == 10
    assert cycle("immediate")["status"] == "not_due"
    assert calls == ["collect", "score"]

    clock.advance(3600)
    assert cycle("hourly")["status"] == "succeeded"
    assert calls == ["collect", "score", "collect"]
    # Use a fresh planner: cadence must survive worker process lifetimes.
    planned = pilot.plan_cycle(tmp_path, config, now=clock.now())
    assert planned["due"] == []
    clock.advance(86400)
    assert cycle("daily")["status"] == "succeeded"
    assert calls == ["collect", "score", "collect", "collect", "score"]


def test_partial_collection_does_not_advance_last_success(tmp_path: Path) -> None:
    clock = Clock()
    config = pilot.PilotConfig()
    partial = False

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        clock.advance(1)
        return {"status": "partial" if partial else "succeeded"}

    pilot.run_cycle(tmp_path, config, "complete", execute=execute,
                    now_fn=clock.now, monotonic=clock.monotonic)
    last_success = state_at(tmp_path)["stages"]["collect"]["last_success_at"]
    clock.advance(3600)
    partial = True
    receipt = pilot.run_cycle(tmp_path, config, "incomplete", execute=execute,
                              now_fn=clock.now, monotonic=clock.monotonic)
    collect = state_at(tmp_path)["stages"]["collect"]
    assert receipt["status"] == collect["last_status"] == "partial"
    assert collect["last_success_at"] == last_success
    assert collect["last_attempt_at"] != last_success
    assert pilot.plan_cycle(tmp_path, config, now=clock.now())["due"] == []


def test_failed_score_preserves_collection_success_and_sanitizes_receipt(tmp_path: Path) -> None:
    clock = Clock()

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        clock.advance(2)
        if stage == "score":
            raise ValueError("upstream credential=must-not-enter-public-receipt")
        return {"status": "succeeded"}

    receipt = pilot.run_cycle(tmp_path, pilot.PilotConfig(), "scorefailed", execute=execute,
                              now_fn=clock.now, monotonic=clock.monotonic)
    state = state_at(tmp_path)
    assert receipt["status"] == "failed"
    assert receipt["error_type"] == "ValueError"
    assert "must-not-enter-public-receipt" not in json.dumps(receipt)
    assert state["stages"]["collect"]["last_success_at"]
    assert state["stages"]["collect"]["last_status"] == "succeeded"
    assert state["stages"]["score"]["last_status"] == "failed"
    assert "last_success_at" not in state["stages"]["score"]
    assert state["active"] is None
    assert not state.get("recovery_required", False)
    assert state["days"]["2026-09-09"] == 4


def test_failed_collection_requires_recovery_before_any_retry(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[str] = []

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        calls.append(stage)
        raise OSError("simulated interrupted append")

    receipt = pilot.run_cycle(tmp_path, pilot.PilotConfig(), "broken", execute=execute,
                              now_fn=clock.now, monotonic=clock.monotonic)
    assert receipt["status"] == "failed"
    assert calls == ["collect"]
    assert state_at(tmp_path)["recovery_required"] is True
    clock.advance(86400)
    planned = pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=clock.now())
    assert planned["recovery_required"] is True
    assert planned["can_run"] is False
    retry = pilot.run_cycle(tmp_path, pilot.PilotConfig(), "retry", execute=execute,
                            now_fn=clock.now, monotonic=clock.monotonic)
    assert retry["status"] == "recovery_required"
    assert calls == ["collect"]


def test_completed_runtime_can_exhaust_the_next_full_reservation(tmp_path: Path) -> None:
    clock = Clock()
    config = pilot.PilotConfig(collect_every_seconds=1, score_every_seconds=1,
                               cycle_timeout_seconds=20, daily_runtime_seconds=20)
    calls: list[str] = []

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        # Reservation must be durable before work begins, including first stage.
        assert state_at(data)["days"]["2026-09-09"] == 20
        calls.append(stage)
        clock.advance(9)
        return {"status": "succeeded"}

    pilot.run_cycle(tmp_path, config, "charged", execute=execute,
                    now_fn=clock.now, monotonic=clock.monotonic)
    assert state_at(tmp_path)["days"]["2026-09-09"] == 18
    retry = pilot.run_cycle(tmp_path, config, "retry", execute=execute,
                            now_fn=clock.now, monotonic=clock.monotonic)
    assert retry["status"] == "budget_exhausted"
    assert retry["plan"]["runtime_remaining_seconds"] == 2
    assert calls == ["collect", "score"]


@pytest.mark.parametrize("interrupted_stage", ["collect", "score"])
def test_crashed_worker_retains_reservation_and_never_marks_success(
    tmp_path: Path, interrupted_stage: str,
) -> None:
    clock = Clock()
    config = pilot.PilotConfig(collect_every_seconds=1, score_every_seconds=1,
                               cycle_timeout_seconds=20, daily_runtime_seconds=20)

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        clock.advance(1)
        if stage == interrupted_stage:
            raise KeyboardInterrupt
        return {"status": "succeeded"}

    with pytest.raises(KeyboardInterrupt):
        pilot.run_cycle(tmp_path, config, "crashed", execute=execute,
                        now_fn=clock.now, monotonic=clock.monotonic)
    abandoned = state_at(tmp_path)
    assert abandoned["active"]["run_id"] == "crashed"
    assert abandoned["days"]["2026-09-09"] == 20
    assert "last_success_at" not in abandoned["stages"][interrupted_stage]
    clock.advance(5)
    result = pilot.run_cycle(tmp_path, config, "replacement", execute=execute,
                             now_fn=clock.now, monotonic=clock.monotonic)
    expected = "recovery_required" if interrupted_stage == "collect" else "budget_exhausted"
    assert result["status"] == expected
    recovered = state_at(tmp_path)
    assert recovered["days"]["2026-09-09"] == 20
    assert recovered["stages"][interrupted_stage]["last_status"] == "interrupted"
    assert recovered["active"] is None
    receipt = json.loads((tmp_path / ".lean-pilot/runs/crashed/receipt.json").read_text())
    assert receipt["status"] == "interrupted"
    assert receipt["reservation_retained"] is True
    if interrupted_stage == "score":
        assert recovered["stages"]["collect"]["last_success_at"]
        assert not recovered.get("recovery_required", False)


def test_midnight_cycle_reserves_both_days_then_refunds_conservatively(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 9, 23, 59, 55, tzinfo=UTC))
    config = pilot.PilotConfig(cycle_timeout_seconds=20, daily_runtime_seconds=40)

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        assert state_at(data)["days"] == {"2026-09-09": 20, "2026-09-10": 20}
        clock.advance(5)
        return {"status": "succeeded"}

    planned = pilot.plan_cycle(tmp_path, config, now=clock.now())
    assert planned["reservation_days"] == ["2026-09-09", "2026-09-10"]
    result = pilot.run_cycle(tmp_path, config, "midnight", execute=execute,
                             now_fn=clock.now, monotonic=clock.monotonic)
    assert result["wall_seconds"] == 10
    assert state_at(tmp_path)["days"] == {"2026-09-09": 10, "2026-09-10": 10}


@pytest.mark.parametrize("raw", [
    "not json",
    '{"schema_version": 99, "days": {}, "stages": {}}',
    '{"schema_version": true, "days": {}, "stages": {}}',
    '{"schema_version": 1, "days": {"2026-09-09": -1}, "stages": {}}',
    '{"schema_version": 1, "days": {"2026-09-09": NaN}, "stages": {}}',
    '{"schema_version": 1, "days": {"2026-09-09": true}, "stages": {}}',
    ('{"schema_version": 1, "days": {}, "stages": {"collect": '
     '{"last_attempt_at": "2026-09-09T12:00:00"}}}'),
    ('{"schema_version": 1, "days": {}, "stages": {}, '
     '"active": {"run_id": "../elsewhere"}}'),
])
def test_corrupt_accounting_fails_closed_without_rewriting_state(tmp_path: Path, raw: str) -> None:
    path = tmp_path / ".lean-pilot/state.json"
    path.parent.mkdir()
    path.write_text(raw, encoding="utf-8")

    def never(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        pytest.fail("Corrupt state must never dispatch work")

    with pytest.raises(ValueError):
        pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=Clock().now())
    with pytest.raises(ValueError):
        pilot.run_cycle(tmp_path, pilot.PilotConfig(), "refused", execute=never)
    assert path.read_text(encoding="utf-8") == raw
    assert not (path.parent / "runs").exists()


def test_cli_plan_does_not_create_the_data_directory(tmp_path: Path) -> None:
    missing = tmp_path / "new-data"
    assert pilot.main(["--data-dir", str(missing), "--plan"]) == 0
    assert not missing.exists()


def test_os_lock_blocks_another_process_and_releases_after_exit(tmp_path: Path) -> None:
    lock = tmp_path / "worker.lock"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from marketsignalos_polymarket.lean_pilot import worker_lock, WorkerBusy\n"
        "try:\n"
        "    with worker_lock(Path(sys.argv[1])):\n"
        "        print('acquired', flush=True)\n"
        "except WorkerBusy:\n"
        "    sys.exit(23)\n"
    )
    with pilot.worker_lock(lock):
        blocked = subprocess.run([sys.executable, "-c", script, str(lock)],
                                 capture_output=True, text=True, timeout=15, check=False)
        assert blocked.returncode == 23, blocked.stderr
        assert "acquired" not in blocked.stdout
    available = subprocess.run([sys.executable, "-c", script, str(lock)],
                               capture_output=True, text=True, timeout=15, check=False)
    assert available.returncode == 0, available.stderr
    assert available.stdout.strip() == "acquired"
    # Child exit must release the OS lock while retaining the same lock file.
    with pilot.worker_lock(lock):
        assert lock.exists()


@pytest.mark.skipif(importlib.util.find_spec("psutil") is None, reason="requires psutil")
@pytest.mark.parametrize(("timeout", "rss_limit", "expected"), [
    (1, 256, "timeout"),
    (10, 1, "rss_limit"),
])
def test_supervisor_stops_small_real_child_process(
    tmp_path: Path, timeout: int, rss_limit: int, expected: str,
) -> None:
    config = pilot.PilotConfig(cycle_timeout_seconds=timeout, rss_limit_mb=rss_limit)
    started = time.monotonic()
    result = pilot.supervise(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path / "worker.log", config,
    )
    assert result["status"] == "failed"
    assert result["stop_reason"] == expected
    assert result["returncode"] != 0
    assert result["sampled_peak_rss_bytes"] > 0
    assert 0 < result["wall_seconds"] < 15
    assert time.monotonic() - started < 15


def test_worker_own_deadline_stops_it_without_a_supervisor(tmp_path: Path) -> None:
    request = tmp_path / "job.json"
    response = tmp_path / "result.json"
    request.write_text(json.dumps({
        "data_dir": str(tmp_path / "data"), "run_id": "watchdog",
        "config": {"cycle_timeout_seconds": 1},
    }), encoding="utf-8")
    # Replace the worker body only: exercise the real internal CLI watchdog
    # without touching collection or any source data, and with no supervisor.
    script = (
        "import sys, time\n"
        "from marketsignalos_polymarket import lean_pilot as pilot\n"
        "pilot.run_cycle = lambda *args: time.sleep(30)\n"
        "sys.exit(pilot.main(['_worker', sys.argv[1], sys.argv[2]]))\n"
    )
    environment = dict(os.environ)
    environment.pop("DATABASE_URL", None)
    child = subprocess.run([sys.executable, "-c", script, str(request), str(response)],
                           capture_output=True, text=True, env=environment, timeout=15, check=False)
    assert child.returncode == 124, child.stderr
    assert not response.exists()
