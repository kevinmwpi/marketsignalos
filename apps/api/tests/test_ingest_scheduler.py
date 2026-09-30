from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import create_app
from marketsignalos_api.services.ingest_scheduler import (
    IngestScheduler,
    ScheduleConfig,
    get_schedule_status,
)


def test_config_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INGEST_EVERY_MINUTES", raising=False)
    assert ScheduleConfig.from_env() is None


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_EVERY_MINUTES", "30")
    monkeypatch.setenv("INGEST_DEEP_EVERY_N_RUNS", "4")
    assert ScheduleConfig.from_env() == ScheduleConfig(
        interval_minutes=30, deep_every_n_runs=4
    )


def test_config_ignores_non_integer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_EVERY_MINUTES", "soon")
    assert ScheduleConfig.from_env() is None


def test_every_nth_dispatch_is_deep() -> None:
    dispatched: list[str] = []

    def trigger(kind: str) -> str | None:
        dispatched.append(kind)
        return "2026-06-11T00:00:00Z"

    scheduler = IngestScheduler(
        ScheduleConfig(interval_minutes=60, deep_every_n_runs=3), trigger
    )
    for _ in range(6):
        scheduler.tick()
    assert dispatched == ["shallow", "shallow", "deep", "shallow", "shallow", "deep"]
    assert scheduler.runs_dispatched == 6


def test_deep_disabled_when_cadence_zero() -> None:
    scheduler = IngestScheduler(
        ScheduleConfig(interval_minutes=60, deep_every_n_runs=0),
        lambda _kind: "t",
    )
    assert [scheduler.tick() for _ in range(3)] == ["shallow"] * 3


def test_busy_skip_retries_same_kind_next_tick() -> None:
    """A tick that finds a run in flight must not advance the deep cadence —
    the skipped deep run is retried, not silently swallowed."""
    busy = True

    def trigger(kind: str) -> str | None:
        return None if busy else "t"

    scheduler = IngestScheduler(
        ScheduleConfig(interval_minutes=60, deep_every_n_runs=1), trigger
    )
    assert scheduler.next_kind() == "deep"
    assert scheduler.tick() is None
    assert scheduler.runs_skipped_busy == 1
    assert scheduler.runs_dispatched == 0

    busy = False
    assert scheduler.tick() == "deep"
    assert scheduler.runs_dispatched == 1


def test_loop_fires_first_dispatch_after_startup_delay() -> None:
    dispatched: list[str] = []

    def trigger(kind: str) -> str | None:
        dispatched.append(kind)
        return "t"

    async def scenario() -> None:
        scheduler = IngestScheduler(
            ScheduleConfig(interval_minutes=1, deep_every_n_runs=0),
            trigger,
            startup_delay_seconds=0.01,
        )
        scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop()

    asyncio.run(scenario())
    # First tick fired after the (shortened) startup delay; the next is a
    # full interval away, so exactly one dispatch lands inside the window.
    assert dispatched == ["shallow"]


def test_status_block_surfaces_through_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the env set, the lifespan starts the scheduler and /ingestor/status
    reports the schedule; shutdown clears it. The 60s startup delay keeps the
    real pipeline from dispatching inside the test."""
    monkeypatch.setenv("INGEST_EVERY_MINUTES", "45")
    monkeypatch.setenv("INGEST_DEEP_EVERY_N_RUNS", "8")
    with TestClient(create_app()) as client:
        schedule = client.get("/ingestor/status").json()["schedule"]
        assert schedule is not None
        assert schedule["enabled"] is True
        assert schedule["interval_minutes"] == 45
        assert schedule["deep_every_n_runs"] == 8
        assert schedule["runs_dispatched"] == 0
        assert schedule["next_run_kind"] == "shallow"
        assert schedule["next_run_at"] is not None
    assert get_schedule_status() is None


def test_status_block_null_when_scheduler_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INGEST_EVERY_MINUTES", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/ingestor/status").json()["schedule"] is None
