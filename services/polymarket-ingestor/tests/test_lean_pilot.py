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


# ── Price backfill stages ─────────────────────────────────────────────────────

def test_backfill_stages_run_between_collect_and_score_on_their_own_cadence(
    tmp_path: Path,
) -> None:
    clock = Clock()
    config = pilot.PilotConfig(entry_prices_every_seconds=6 * 3600,
                               closing_lines_every_seconds=12 * 3600)
    calls: list[str] = []

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        calls.append(stage)
        clock.advance(1)
        return {"status": "succeeded"}

    def cycle(run: str) -> dict[str, Any]:
        return pilot.run_cycle(tmp_path, config, run, execute=execute,
                               now_fn=clock.now, monotonic=clock.monotonic)

    assert cycle("first")["status"] == "succeeded"
    assert calls == ["collect", "entry_prices", "closing_lines", "score"]
    clock.advance(3600)
    cycle("hourly")
    assert calls[4:] == ["collect"]  # the backfills wait for their own intervals
    clock.advance(5 * 3600)
    cycle("sixhourly")
    assert calls[5:] == ["collect", "entry_prices"]
    clock.advance(6 * 3600)
    cycle("twelvehourly")
    assert calls[7:] == ["collect", "entry_prices", "closing_lines"]
    stages = state_at(tmp_path)["stages"]
    assert stages["entry_prices"]["last_status"] == "succeeded"
    assert stages["closing_lines"]["last_status"] == "succeeded"


def test_backfill_stages_are_off_unless_configured(tmp_path: Path) -> None:
    planned = pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=Clock().now())
    assert planned["due"] == ["collect", "score"]


@pytest.mark.parametrize("stage", ["entry_prices", "closing_lines", "horizon"])
def test_an_interrupted_backfill_run_needs_no_recovery(tmp_path: Path, stage: str) -> None:
    clock = Clock()
    fields: dict[str, Any] = {f"{stage}_every_seconds": 3600}
    config = pilot.PilotConfig(**fields)

    def execute(name: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        if name == stage:
            raise RuntimeError("upstream failure")
        return {"status": "succeeded"}

    receipt = pilot.run_cycle(tmp_path, config, "broken", execute=execute,
                              now_fn=clock.now, monotonic=clock.monotonic)
    assert receipt["status"] == "failed"
    state = state_at(tmp_path)
    assert state["stages"][stage]["last_status"] == "failed"
    assert state.get("recovery_required", False) is False  # append-only: nothing to reconcile


@pytest.mark.parametrize("fields", [
    {"closing_lines_every_seconds": 3600, "closing_lines_max_seconds": 601},
    {"entry_prices_every_seconds": 3600, "entry_prices_max_seconds": 601},
    # Each fits alone; together they would take more than half the cycle.
    {"entry_prices_every_seconds": 3600, "entry_prices_max_seconds": 400,
     "closing_lines_every_seconds": 3600, "closing_lines_max_seconds": 201},
    {"closing_lines_per_cycle": 0},
    {"closing_lines_max_seconds": 0},
    {"closing_lines_every_seconds": -1},
    {"entry_prices_per_cycle": 0},
    {"entry_prices_max_seconds": 0},
    {"entry_prices_every_seconds": -1},
])
def test_backfill_limits_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        pilot.PilotConfig(**fields)


def test_a_disabled_backfill_does_not_count_against_the_cycle() -> None:
    config = pilot.PilotConfig(entry_prices_every_seconds=3600, entry_prices_max_seconds=600,
                               closing_lines_max_seconds=600)
    assert config.closing_lines_every_seconds == 0


def test_the_deployed_config_is_valid_and_enables_both_backfills() -> None:
    path = Path(__file__).resolve().parents[3] / "deploy" / "lean-pilot.json"
    config = pilot.PilotConfig(**json.loads(path.read_text(encoding="utf-8")))
    assert config.entry_prices_every_seconds > 0
    assert config.closing_lines_every_seconds > 0
    assert 0 < config.horizon_every_seconds <= 86400
    assert config.horizon_every_seconds == 86400  # daily since the decision (2026-10-05)
    # The backfills share the daily runtime allowance with hourly collection, so
    # their combined worst case must stay a small part of it.
    worst_case = sum(86400 // every * max_seconds for every, max_seconds in (
        (config.entry_prices_every_seconds, config.entry_prices_max_seconds),
        (config.closing_lines_every_seconds, config.closing_lines_max_seconds)))
    assert worst_case <= config.daily_runtime_seconds // 3


# ── Position retention after collection ──────────────────────────────────────

def test_collection_retention_keeps_two_snapshots_and_the_exit_watermark(
    tmp_path: Path,
) -> None:
    path = tmp_path / "polymarket_positions.jsonl"
    path.write_text("".join(
        json.dumps({"proxy_wallet": "0xa", "condition_id": "0xc", "size": 1.0,
                    "snapshot_id": f"s{i}", "snapshot_at": f"2026-10-02T0{i}:00:00+00:00"}) + "\n"
        for i in range(1, 5)), encoding="utf-8")
    (tmp_path / "exit_state.json").write_text(json.dumps({"0xa": "s1"}), encoding="utf-8")

    result = pilot._compact_positions(tmp_path)

    assert (result["rows_before"], result["rows_after"]) == (4, 3)
    kept = [json.loads(line)["snapshot_id"] for line in path.read_text().splitlines()]
    assert kept == ["s1", "s3", "s4"]


def test_collection_retention_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import storage

    def disk_full(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "compact_position_snapshots", disk_full)
    assert pilot._compact_positions(tmp_path) == {"error_type": "OSError"}


# ── Disk guard and storage report ────────────────────────────────────────────

def test_a_cycle_does_not_start_on_a_nearly_full_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(pilot, "_disk_free_mb", lambda data_dir: 300)

    def execute(stage: str, data: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        raise AssertionError("no stage may run on a nearly full volume")

    receipt = pilot.run_cycle(tmp_path, pilot.PilotConfig(min_free_disk_mb=512), "full",
                              execute=execute, now_fn=clock.now, monotonic=clock.monotonic)

    assert receipt["status"] == "disk_low"
    assert receipt["plan"]["disk_free_mb"] == 300 and receipt["plan"]["can_run"] is False
    assert "disk_low" in pilot._ALERTING_STATUSES  # Railway sees a failed run
    state_file = tmp_path / ".lean-pilot" / "state.json"
    assert not state_file.exists() or state_at(tmp_path)["days"] == {}  # nothing charged


def test_disk_free_walks_up_to_an_existing_parent(tmp_path: Path) -> None:
    assert pilot._disk_free_mb(tmp_path / "not" / "created" / "yet") > 0


def test_storage_report_sizes_each_store(tmp_path: Path) -> None:
    (tmp_path / "polymarket_activity.jsonl").write_bytes(b"x" * 3 * 1024 * 1024)
    (tmp_path / "entry_prices").mkdir()
    (tmp_path / "entry_prices" / "price_observations.jsonl").write_bytes(b"x" * 1024 * 1024)
    report = pilot._storage_mb(tmp_path)
    assert report["activity"] == 3.0 and report["entry_prices"] == 1.0
    assert report["positions"] == 0.0 and report["total"] == 4.0


def test_the_watchlist_cap_reaches_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import runner

    seen: dict[str, Any] = {}

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"windows_succeeded": ["day"], "wallets_with_errors": 0}

    def fake_pipeline(**kwargs: Any) -> Result:
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    pilot._execute_stage("collect", tmp_path, pilot.PilotConfig(max_watchlist_wallets=64), "r1")
    assert seen["max_watchlist"] == 64
    pilot._execute_stage("collect", tmp_path, pilot.PilotConfig(), "r2")
    assert seen["max_watchlist"] is None  # 0 means no cap
    with pytest.raises(ValueError):
        pilot.PilotConfig(max_watchlist_wallets=-1)


def test_the_horizon_diagnostic_runs_after_the_backfills_and_before_scoring(
    tmp_path: Path,
) -> None:
    config = pilot.PilotConfig(entry_prices_every_seconds=3600, closing_lines_every_seconds=3600,
                               horizon_every_seconds=86400)
    assert pilot.plan_cycle(tmp_path, config, now=Clock().now())["due"] == [
        "collect", "entry_prices", "closing_lines", "horizon", "score"]


def test_the_gate13_diagnostic_runs_last_even_when_cohort_maintenance_is_added(
    tmp_path: Path,
) -> None:
    config = pilot.PilotConfig(cohort_every_seconds=86400, gate13_every_seconds=86400)
    assert pilot.plan_cycle(tmp_path, config, now=Clock().now())["due"][-3:] == [
        "score", "cohort", "gate13"]
    clock = Clock(datetime(2026, 10, 5, 0, 9, tzinfo=UTC))
    pilot.run_cycle(tmp_path, config, "first", execute=lambda *a: {},
                    now_fn=clock.now, monotonic=clock.monotonic)
    snapshots = tmp_path / "score-snapshots"
    snapshots.mkdir()
    (snapshots / "current.json").write_text(json.dumps({"run_id": "unread"}))
    later = datetime(2026, 10, 6, 0, 9, tzinfo=UTC)  # gate13 due by interval, score too
    assert pilot.plan_cycle(tmp_path, config, now=later)["due"][-3:] == [
        "score", "cohort", "gate13"]
    hourly = datetime(2026, 10, 5, 9, 10, tzinfo=UTC)  # only the unread score adds cohort
    assert pilot.plan_cycle(tmp_path, config, now=hourly)["due"] == ["collect", "cohort"]
    assert "gate13" not in pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=later)["due"]


def test_the_score_stage_publishes_the_configured_score_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import score_snapshot

    seen: dict[str, Any] = {}

    def fake(data_dir: Path, snapshots_dir: Path, run_id: str, *,
             score_version: str) -> dict[str, Any]:
        seen.update(run_id=run_id, score_version=score_version)
        return {"status": "succeeded"}

    monkeypatch.setattr(score_snapshot, "score_snapshot", fake)
    pilot._execute_stage("score", tmp_path, pilot.PilotConfig(score_version="forecast-v5"), "r")
    assert seen == {"run_id": "r", "score_version": "forecast-v5"}
    assert pilot.PilotConfig().score_version == "forecast-v4"
    with pytest.raises(ValueError, match="score_version"):
        pilot.PilotConfig(score_version="forecast-v6")


def test_the_horizon_stage_writes_a_report_from_an_empty_directory(tmp_path: Path) -> None:
    result = pilot._execute_stage("horizon", tmp_path, pilot.PilotConfig(), "r1")
    assert result["status"] == "succeeded" and result["resolved_bets"] == 0
    assert list((tmp_path / "diagnostics" / "horizon").glob("*.json"))


def test_a_stage_is_due_a_few_minutes_early_so_cron_drift_skips_no_hour(tmp_path: Path) -> None:
    """Railway fired at 22:10:24 after an attempt at 21:12:46; without grace the
    hourly stage waited until 23:10."""
    clock = Clock(datetime(2026, 10, 2, 21, 12, 46, tzinfo=UTC))
    pilot.run_cycle(tmp_path, pilot.PilotConfig(), "first", execute=lambda *a: {},
                    now_fn=clock.now, monotonic=clock.monotonic)
    config = pilot.PilotConfig()
    early = datetime(2026, 10, 2, 22, 10, 24, tzinfo=UTC)
    assert "collect" in pilot.plan_cycle(tmp_path, config, now=early)["due"]
    too_soon = datetime(2026, 10, 2, 22, 0, 0, tzinfo=UTC)  # more than 10 minutes early
    assert pilot.plan_cycle(tmp_path, config, now=too_soon)["due"] == []
    assert pilot._due_grace_seconds(3600) == 600 and pilot._due_grace_seconds(600) == 150


def test_the_worker_hands_its_metadata_limits_to_the_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_cycle(data_dir: Path, config: pilot.PilotConfig, run_id: str) -> dict[str, Any]:
        seen["conditions"] = os.environ["METADATA_BACKFILL_MAX_CONDITIONS"]
        seen["requests"] = os.environ["METADATA_BACKFILL_MAX_REQUESTS"]
        return {"run_id": run_id, "status": "succeeded"}

    for name in ("POLYMARKET_DATA_DIR", "POLYMARKET_WATCHLIST_PATH", "POLYMARKET_WALLET_CONCURRENCY",
                 "POLYMARKET_API_RPS", "METADATA_BACKFILL_MAX_CONDITIONS",
                 "METADATA_BACKFILL_MAX_REQUESTS"):
        monkeypatch.setenv(name, "restored-after-the-test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pilot, "run_cycle", fake_cycle)
    request = tmp_path / "job.json"
    request.write_text(json.dumps({"data_dir": str(tmp_path / "data"), "run_id": "limits", "config": {
        "metadata_conditions_per_cycle": 600, "metadata_requests_per_cycle": 48}}),
        encoding="utf-8")

    assert pilot.main(["_worker", str(request), str(tmp_path / "result.json")]) == 0
    assert seen == {"conditions": "600", "requests": "48"}
    with pytest.raises(ValueError):
        pilot.PilotConfig(metadata_conditions_per_cycle=1001)


def test_collection_uses_the_seed_window_and_skips_excluded_wallets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import cohort, runner

    seen: dict[str, Any] = {}

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"windows_succeeded": ["month"], "wallets_with_errors": 0}

    def fake_pipeline(**kwargs: Any) -> Result:
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    cohort.record_exclusions(tmp_path, {"0xbot": "systematic"},
                             now=datetime(2026, 10, 3, tzinfo=UTC))
    config = pilot.PilotConfig(leaderboard_window="month", leaderboard_metric="profit")
    pilot._execute_stage("collect", tmp_path, config, "r")
    assert seen["windows"] == ["month"] and seen["exclude_wallets"] == {"0xbot"}
    assert seen["seed_metrics"] == ("profit",)
    pilot._execute_stage("collect", tmp_path, pilot.PilotConfig(), "r")
    assert seen["seed_metrics"] == ("volume",)  # the default, as before
    with pytest.raises(ValueError):
        pilot.PilotConfig(leaderboard_window="year")
    with pytest.raises(ValueError):
        pilot.PilotConfig(leaderboard_metric="roi")


def test_cohort_maintenance_runs_right_after_scoring(tmp_path: Path) -> None:
    config = pilot.PilotConfig(cohort_every_seconds=86400)
    due = pilot.plan_cycle(tmp_path, config, now=Clock().now())["due"]
    assert due[-2:] == ["score", "cohort"]

    # 2026-10-03: the stage first ran at 00:09, so on its own interval it lagged the
    # 08:12 score by 16 hours. Now it runs with every score, and in the first cycle
    # after a score it has not acted on.
    clock = Clock(datetime(2026, 10, 3, 0, 9, tzinfo=UTC))
    pilot.run_cycle(tmp_path, config, "first", execute=lambda *a: {},
                    now_fn=clock.now, monotonic=clock.monotonic)
    later = datetime(2026, 10, 4, 0, 9, tzinfo=UTC)
    assert pilot.plan_cycle(tmp_path, config, now=later)["due"][-2:] == ["score", "cohort"]
    hourly = datetime(2026, 10, 3, 9, 10, tzinfo=UTC)  # neither interval is due
    assert "cohort" not in pilot.plan_cycle(tmp_path, config, now=hourly)["due"]
    snapshots = tmp_path / "score-snapshots"
    snapshots.mkdir()
    (snapshots / "current.json").write_text(json.dumps({"run_id": "r0812"}))
    assert pilot.plan_cycle(tmp_path, config, now=hourly)["due"][-1] == "cohort"
    assert "cohort" not in pilot.plan_cycle(  # off means off, unprocessed score or not
        tmp_path, pilot.PilotConfig(), now=hourly)["due"]
    (tmp_path / "cohort_state.json").write_text(json.dumps({"score_run_id": "r0812"}))
    assert "cohort" not in pilot.plan_cycle(tmp_path, config, now=hourly)["due"]
    path = Path(__file__).resolve().parents[3] / "deploy" / "lean-pilot.json"
    deployed = pilot.PilotConfig(**json.loads(path.read_text(encoding="utf-8")))
    assert deployed.cohort_every_seconds == 86400 and deployed.leaderboard_window == "month"
    assert deployed.leaderboard_metric == "profit"  # since 2026-10-03
    assert deployed.score_version == "forecast-v4"  # v5 waits for plan step 4
    assert deployed.gate13_every_seconds == 86400  # plan step 3, daily
    # Deep enough that freed slots refill up to the cap (2026-10-04).
    assert deployed.leaderboard_limit == 100 and deployed.max_watchlist_wallets == 64
