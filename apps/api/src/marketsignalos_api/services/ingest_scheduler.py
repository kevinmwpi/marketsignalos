"""
Background ingest scheduler.

When INGEST_EVERY_MINUTES is set (> 0) the API process dispatches the same
in-process pipeline trigger the dashboard buttons use, on a fixed interval.
INGEST_DEEP_EVERY_N_RUNS (> 0) makes every Nth scheduled run a deep
discovery pass (categorized leaderboard sweep + recent-trader discovery)
instead of a shallow refresh — e.g. INGEST_EVERY_MINUTES=60 with
INGEST_DEEP_EVERY_N_RUNS=24 gives an hourly refresh and one discovery sweep
per day.

The scheduler runs as a single asyncio task inside the FastAPI lifespan —
no extra process, no cron infra. Overlap is impossible by construction:
dispatch goes through the ingestor route's shared running flag, and a tick
that finds a run still in flight just skips (the skip is counted and visible
in /ingestor/status). The first dispatch waits ~60s after startup so a
crash-looping deploy can't hammer the upstream APIs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger("marketsignalos.api.ingest_scheduler")

STARTUP_DELAY_SECONDS = 60.0


def _env_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-integer %s=%r", name, raw)
        return 0


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    interval_minutes: int
    deep_every_n_runs: int  # 0 = scheduled runs are always shallow

    @classmethod
    def from_env(cls) -> ScheduleConfig | None:
        """None (scheduler off) unless INGEST_EVERY_MINUTES is a positive int."""
        interval = _env_int("INGEST_EVERY_MINUTES")
        if interval <= 0:
            return None
        return cls(
            interval_minutes=interval,
            deep_every_n_runs=max(0, _env_int("INGEST_DEEP_EVERY_N_RUNS")),
        )


class IngestScheduler:
    """Dispatches pipeline runs on a fixed interval via an injected trigger.

    `trigger(kind)` must return the run's started_at when dispatched and
    None when a run is already in flight — the contract of
    routes.ingestor.start_pipeline_run. A busy skip does not advance the
    deep-run counter, so the skipped kind is retried on the next tick.
    """

    def __init__(
        self,
        config: ScheduleConfig,
        trigger: Callable[[str], str | None],
        *,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._config = config
        self._trigger = trigger
        self._startup_delay_seconds = startup_delay_seconds
        self._task: asyncio.Task[None] | None = None
        self.runs_dispatched = 0
        self.runs_skipped_busy = 0
        self.next_run_at: datetime | None = None

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    def next_kind(self) -> str:
        """Kind of the next dispatch: every Nth (1-indexed) is deep."""
        n = self._config.deep_every_n_runs
        if n > 0 and (self.runs_dispatched + 1) % n == 0:
            return "deep"
        return "shallow"

    def tick(self) -> str | None:
        """One scheduling decision: dispatch the next run unless one is
        already in flight. Returns the dispatched kind, or None on skip."""
        kind = self.next_kind()
        started_at = self._trigger(kind)
        if started_at is None:
            self.runs_skipped_busy += 1
            log.info(
                "scheduled %s run skipped: previous run still in flight", kind
            )
            return None
        self.runs_dispatched += 1
        log.info(
            "scheduled %s run dispatched started_at=%s run_number=%d",
            kind,
            started_at,
            self.runs_dispatched,
        )
        return kind

    async def _loop(self) -> None:
        interval_seconds = self._config.interval_minutes * 60.0
        delay = min(self._startup_delay_seconds, interval_seconds)
        while True:
            self.next_run_at = datetime.now(UTC) + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            self.tick()
            delay = interval_seconds

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval_minutes": self._config.interval_minutes,
            "deep_every_n_runs": self._config.deep_every_n_runs,
            "runs_dispatched": self.runs_dispatched,
            "runs_skipped_busy": self.runs_skipped_busy,
            "next_run_kind": self.next_kind(),
            "next_run_at": (
                self.next_run_at.isoformat() if self.next_run_at else None
            ),
        }


_active: IngestScheduler | None = None


def start_scheduler_from_env() -> IngestScheduler | None:
    """Start the env-configured scheduler (called from the API lifespan).
    Returns None — and says why — when INGEST_EVERY_MINUTES is unset."""
    global _active
    config = ScheduleConfig.from_env()
    if config is None:
        log.info("ingest scheduler disabled (INGEST_EVERY_MINUTES not set)")
        _active = None
        return None

    # Imported here, not at module top: the ingestor route imports this
    # module for get_schedule_status, so a top-level import would be a cycle.
    from marketsignalos_api.api.routes.ingestor import start_pipeline_run

    scheduler = IngestScheduler(config, start_pipeline_run)
    scheduler.start()
    _active = scheduler
    log.info(
        "ingest scheduler started interval_minutes=%d deep_every_n_runs=%d",
        config.interval_minutes,
        config.deep_every_n_runs,
    )
    return scheduler


async def stop_scheduler() -> None:
    global _active
    if _active is not None:
        await _active.stop()
        _active = None


def get_schedule_status() -> dict[str, Any] | None:
    """Schedule block for /ingestor/status; None when the scheduler is off."""
    return _active.status() if _active is not None else None
