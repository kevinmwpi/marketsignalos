"""A one-shot, locally locked pilot worker; no cloud resources are provisioned.

The subprocess owns the lock and durable runtime reservation. If its supervisor
dies, a replacement cannot overlap it. A killed worker keeps its full reservation
charged until the UTC day expires, and never advances the success watermark.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("marketsignalos.lean_pilot")
SCHEMA_VERSION = 1


# Fields where 0 means "off" or "no limit".
_ZERO_ALLOWED = frozenset({"entry_prices_every_seconds", "closing_lines_every_seconds",
                           "horizon_every_seconds", "cohort_every_seconds",
                           "gate13_every_seconds", "max_watchlist_wallets",
                           "cohort_v1_every_seconds"})
_LEADERBOARD_WINDOWS = frozenset({"day", "week", "month", "all"})
_LEADERBOARD_METRICS = frozenset({"volume", "profit"})
_SCORE_VERSIONS = frozenset({"forecast-v4", "forecast-v5"})
_TEXT_FIELDS = frozenset({"leaderboard_window", "leaderboard_metric", "score_version",
                          "cohort_v1_freeze_at"})


@dataclass(frozen=True)
class PilotConfig:
    collect_every_seconds: int = 3600
    score_every_seconds: int = 86400
    wallet_batch_size: int = 20
    activity_requests_per_wallet: int = 10
    leaderboard_limit: int = 25
    cycle_timeout_seconds: int = 1200
    daily_runtime_seconds: int = 3600
    rss_limit_mb: int = 4096
    log_limit_mb: int = 8
    # Price backfills for CLV research. 0 disables a stage; the deployed config
    # turns them on. Each run fetches at most `per_cycle` items and starts none
    # after `max_seconds`. Entry prices (entry_prices.py) hold the hours after
    # each buy, the gate-13 reference chosen on 2026-09-30; closing lines
    # (closing_lines.py) hold each market's 48 hours before close.
    entry_prices_every_seconds: int = 0
    entry_prices_per_cycle: int = 200
    entry_prices_max_seconds: int = 180
    closing_lines_every_seconds: int = 0
    closing_lines_per_cycle: int = 150
    closing_lines_max_seconds: int = 240
    # No cycle starts with less free space than this on the data volume. A write
    # that fails mid-collection leaves recovery_required set; skipping is safe.
    min_free_disk_mb: int = 512
    # Gate-13 horizon diagnostic (horizon_diagnostic.py): reads the stores and
    # writes one small report a day. 0 disables it.
    horizon_every_seconds: int = 0
    # Market metadata fetched per collection (metadata_backfill.py): conditions
    # and Gamma requests. Each request covers 25 conditions under one of the two
    # closed filters, so N conditions need 2 * ceil(N / 25) requests.
    metadata_conditions_per_cycle: int = 100
    metadata_requests_per_cycle: int = 8
    # Cohort maintenance (cohort.py): wallets the scorer labels systematic are
    # excluded for good and their data deleted. Besides this interval it runs in
    # every cycle that scores, and in the first cycle after a score it has not
    # acted on yet. 0 disables it.
    cohort_every_seconds: int = 0
    # Leaderboard window and ranking that seed the watchlist. Profit ranking selects
    # wallets on recent winning, so backward-looking results from a profit-seeded
    # cohort are not evidence (blueprint decision 6, 2026-10-03).
    leaderboard_window: str = "day"
    leaderboard_metric: str = "volume"
    # Scorer published by the score stage. forecast-v5 measures gate 13's CLV 1 h
    # after each buy (post_entry_clv.py, docs/gate13-clv-v5-plan.md); the switch
    # is plan step 4, taken with before/after cohort counts.
    score_version: str = "forecast-v4"
    # Gate-13 power diagnostic (gate13_power.py, plan step 3): scores v4 and v5
    # into a scratch directory and reports per-wallet v5 CLV power. Read-only
    # apart from its report. 0 disables it.
    gate13_every_seconds: int = 0
    # The volume cannot grow past 5 GB on Railway Hobby, and every wallet added
    # keeps its activity history, so the watchlist stops growing here. 0 = no cap.
    max_watchlist_wallets: int = 0
    # Stage 3 cohort v1 (cohort_v1.py, docs/stage3-cohort-v1-plan.md). Until the
    # freeze, a provisional member list is rebuilt on this interval so the hourly
    # polling of members runs during the burn-in; 0 disables it. At
    # cohort_v1_freeze_at (ISO-8601 with a timezone, set by the owner) the frozen
    # config is written once and membership is fixed for the window.
    cohort_v1_every_seconds: int = 0
    cohort_v1_freeze_at: str = ""

    def freeze_at(self) -> datetime | None:
        if not self.cohort_v1_freeze_at:
            return None
        moment = datetime.fromisoformat(self.cohort_v1_freeze_at)
        if moment.tzinfo is None:
            raise ValueError("cohort_v1_freeze_at must include a timezone")
        return moment.astimezone(UTC)

    def __post_init__(self) -> None:
        if self.leaderboard_window not in _LEADERBOARD_WINDOWS:
            raise ValueError(f"leaderboard_window must be one of {sorted(_LEADERBOARD_WINDOWS)}")
        if self.leaderboard_metric not in _LEADERBOARD_METRICS:
            raise ValueError(f"leaderboard_metric must be one of {sorted(_LEADERBOARD_METRICS)}")
        if self.score_version not in _SCORE_VERSIONS:
            raise ValueError(f"score_version must be one of {sorted(_SCORE_VERSIONS)}")
        for name, value in asdict(self).items():
            if name in _TEXT_FIELDS:
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            if value == 0 and name not in _ZERO_ALLOWED:
                raise ValueError(f"{name} must be a positive integer")
        backfill_seconds = (
            (self.entry_prices_max_seconds if self.entry_prices_every_seconds else 0)
            + (self.closing_lines_max_seconds if self.closing_lines_every_seconds else 0))
        if backfill_seconds > self.cycle_timeout_seconds // 2:
            raise ValueError("Enabled backfill stages may use at most half of a cycle")
        if (self.metadata_conditions_per_cycle > 1000
                or self.metadata_requests_per_cycle > 80):
            raise ValueError("metadata backfill limits exceed metadata_backfill's ceilings")
        if self.daily_runtime_seconds < self.cycle_timeout_seconds:
            raise ValueError("daily_runtime_seconds must cover one complete cycle reservation")
        if self.cycle_timeout_seconds > 3600:
            raise ValueError("Pilot cycles must be at most one hour")
        if self.wallet_batch_size > 100 or self.leaderboard_limit > 100:
            raise ValueError("Pilot wallet batch and leaderboard limits must be at most 100")
        self.freeze_at()  # validates the timestamp
        if self.cohort_v1_freeze_at and not self.cohort_v1_every_seconds:
            raise ValueError("cohort_v1_freeze_at needs the cohort_v1 stage enabled")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Pilot state timestamps must include a timezone")
    return result.astimezone(UTC)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    """OS-released lock, valid on one host/volume; never delete the lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerBusy("Another pilot worker owns this data directory") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "days": {}, "stages": {}, "active": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
            or state.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("Unsupported pilot state; refusing to reset runtime accounting")
    days = state.get("days")
    stages = state.get("stages")
    if not isinstance(days, dict) or not isinstance(stages, dict):
        raise ValueError("Invalid pilot state")
    for day, seconds in days.items():
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Runtime days must use YYYY-MM-DD")
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds < 0):
            raise ValueError("Invalid runtime accounting")
    for stage, row in stages.items():
        if stage not in STAGES or not isinstance(row, dict):
            raise ValueError("Invalid stage state")
        for key in ("last_attempt_at", "last_success_at"):
            if row.get(key) is not None:
                _timestamp(row[key])
    active = state.get("active")
    if type(state.get("recovery_required", False)) is not bool:
        raise ValueError("Invalid recovery state")
    recovery_run = state.get("recovery_run_id")
    if recovery_run is not None and (not isinstance(recovery_run, str)
                                     or not recovery_run.isalnum()):
        raise ValueError("Invalid recovery run identifier")
    if active is not None:
        if not isinstance(active, dict) or not isinstance(active.get("run_id"), str):
            raise ValueError("Invalid active run")
        if not active["run_id"].isalnum():
            raise ValueError("Invalid active run identifier")
    return state


def _due_grace_seconds(interval: int) -> int:
    """How early a stage may start. Railway's hourly cron fires anywhere from :07
    to about :12, so with no grace a run that fires a few minutes earlier than the
    last attempt skips the stage for an hour: about one hourly collection in three
    was lost that way before 2026-10-02. A quarter of the interval, at most ten
    minutes, absorbs that drift and can never make a stage run twice an hour."""
    return min(600, interval // 4)


# Exit nonzero so Railway marks the run failed and its notifications fire.
_ALERTING_STATUSES = frozenset({"failed", "recovery_required", "disk_low"})


def _disk_free_mb(data_dir: Path) -> int:
    """Free space on the volume holding ``data_dir`` (or its nearest existing parent)."""
    path = data_dir.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free // (1024 * 1024)


# Disk use per store, reported after each collection so growth is visible in
# the Railway log line without access to the volume.
_STORAGE_AREAS = {
    "activity": ("polymarket_activity.jsonl", "polymarket_activity.jsonl.archive"),
    "positions": ("polymarket_positions.jsonl",),
    "entry_prices": ("entry_prices",),
    "closing_lines": ("closing_lines",),
    "score_snapshots": ("score-snapshots",),
    "run_logs": (".lean-pilot",),
}


def _storage_mb(data_dir: Path) -> dict[str, float]:
    def size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        return 0

    sizes = {area: sum(size(data_dir / name) for name in names)
             for area, names in _STORAGE_AREAS.items()}
    sizes["total"] = size(data_dir)
    return {area: round(value / (1024 * 1024), 1) for area, value in sizes.items()}


# Run order within a cycle: collection first, then the price backfills for what
# was collected, then the diagnostic that reads them, then scoring, then cohort
# maintenance, which acts on the trading styles scoring has just labelled, then
# the gate-13 power diagnostic on the cohort that remains.
STAGES = ("collect", "entry_prices", "closing_lines", "horizon", "score", "cohort",
          "cohort_v1", "gate13")


def _stage_intervals(config: PilotConfig) -> list[tuple[str, int]]:
    intervals = {"collect": config.collect_every_seconds,
                 "entry_prices": config.entry_prices_every_seconds,
                 "closing_lines": config.closing_lines_every_seconds,
                 "horizon": config.horizon_every_seconds,
                 "score": config.score_every_seconds,
                 "cohort": config.cohort_every_seconds,
                 "cohort_v1": config.cohort_v1_every_seconds,
                 "gate13": config.gate13_every_seconds}
    return [(name, intervals[name]) for name in STAGES if intervals[name] > 0]


def plan_cycle(data_dir: Path, config: PilotConfig, *,
               now: datetime | None = None) -> dict[str, Any]:
    """Read-only planning. Attempt cadence limits retries; success is separate."""
    now = now or _utcnow()
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(UTC)
    state = _read_state(data_dir / ".lean-pilot" / "state.json")
    due = []
    stages: dict[str, Any] = {}
    for name, interval in _stage_intervals(config):
        row = state["stages"].get(name, {})
        attempted = row.get("last_attempt_at")
        next_due = _timestamp(attempted) + timedelta(seconds=interval) if attempted else now
        if now >= next_due - timedelta(seconds=_due_grace_seconds(interval)):
            due.append(name)
        stages[name] = {**row, "next_due_at": next_due.isoformat()}
    if config.cohort_every_seconds and "cohort" not in due:
        # Act on the trading styles scoring labels as soon as they exist. On its
        # own interval alone it ran 16 hours after scoring on 2026-10-03.
        from .cohort import has_unprocessed_score
        if "score" in due or has_unprocessed_score(data_dir):
            due = [stage for stage in STAGES if stage in due or stage == "cohort"]
    freeze_at = config.freeze_at()
    if (freeze_at is not None and now >= freeze_at and "cohort_v1" not in due
            and not (data_dir / "cohort-v1" / "frozen-config.json").exists()):
        # The freeze runs at the first cycle after its time, whatever the interval.
        due = [stage for stage in STAGES if stage in due or stage == "cohort_v1"]
    # Reserve in both days for a cycle that could cross UTC midnight. This is
    # deliberately conservative; successful completion refunds unused time.
    days = sorted({now.date().isoformat(),
                   (now + timedelta(seconds=config.cycle_timeout_seconds)).date().isoformat()})
    remaining = min(config.daily_runtime_seconds - state["days"].get(day, 0) for day in days)
    disk_free_mb = _disk_free_mb(data_dir)
    return {"schema_version": SCHEMA_VERSION, "budget_target_usd_month": 15,
            "budget_is_billing_cap": False, "config": asdict(config), "due": due,
            "stages": stages, "reservation_days": days,
            "runtime_remaining_seconds": max(0, remaining),
            "disk_free_mb": disk_free_mb,
            "recovery_required": state.get("recovery_required", False),
            # The id an operator passes to --recover (PILOT_RECOVER); see pilot_recovery.
            "recovery_run_id": state.get("recovery_run_id"),
            "can_run": bool(due) and remaining >= config.cycle_timeout_seconds
            and disk_free_mb >= config.min_free_disk_mb
            and not state.get("recovery_required", False)}


StageRunner = Callable[[str, Path, PilotConfig, str], dict[str, Any]]


def _execute_stage(stage: str, data_dir: Path, config: PilotConfig,
                   run_id: str) -> dict[str, Any]:
    if stage == "score":
        from .score_snapshot import score_snapshot
        return score_snapshot(data_dir, data_dir / "score-snapshots", run_id,
                              score_version=config.score_version)
    # The backfills are append-only and safe to interrupt: unlike collection, a
    # killed run leaves nothing to reconcile, so they never set recovery_required.
    if stage == "entry_prices":  # gate 13's chunks first: the diagnostic's and v5's
        from . import entry_prices, horizon_diagnostic, post_entry_clv
        priority = (horizon_diagnostic.priority_chunks(data_dir)
                    | post_entry_clv.priority_chunks(data_dir))
        return entry_prices.run_pending(data_dir, limit=config.entry_prices_per_cycle,
                                        max_seconds=config.entry_prices_max_seconds,
                                        priority=priority)
    if stage == "cohort":  # after scoring, which labels each wallet's trading style
        from . import cohort, cohort_v1
        return cohort.run(data_dir, protected=cohort_v1.frozen_members(data_dir))
    if stage == "cohort_v1":  # provisional members, or the one-time freeze
        from . import cohort_v1
        return cohort_v1.run(data_dir, now=_utcnow(), freeze_at=config.freeze_at(),
                             discovery={"leaderboard_metric": config.leaderboard_metric,
                                        "leaderboard_window": config.leaderboard_window,
                                        "leaderboard_limit": config.leaderboard_limit,
                                        "max_watchlist_wallets": config.max_watchlist_wallets})
    if stage == "gate13":  # read-only apart from its report and a scratch directory
        from . import gate13_power
        return gate13_power.run(data_dir)
    if stage == "horizon":  # read-only apart from its own report
        from . import horizon_diagnostic
        return horizon_diagnostic.run(data_dir)
    if stage == "closing_lines":  # the horizon decision's markets first
        from . import closing_lines, horizon_diagnostic
        return closing_lines.run_pending(data_dir, limit=config.closing_lines_per_cycle,
                                         max_seconds=config.closing_lines_max_seconds,
                                         priority=horizon_diagnostic.priority_markets(data_dir))
    from .cohort import excluded_wallets
    from .cohort_capture import activity_offset
    from .cohort_v1 import member_wallets
    from .runner import run_pipeline
    members = member_wallets(data_dir)
    offset = activity_offset(data_dir)  # rows past it are this run's (Stage 3 capture)
    result = run_pipeline(
        windows=[config.leaderboard_window], leaderboard_limit=config.leaderboard_limit,
        seed_metrics=(config.leaderboard_metric,),
        wallet_batch_size=config.wallet_batch_size, skip_enrichment=True,
        max_pages_per_wallet=2, market_pages=1, refresh_reference=False,
        max_activity_requests_per_wallet=config.activity_requests_per_wallet,
        max_watchlist=config.max_watchlist_wallets or None,
        exclude_wallets=excluded_wallets(data_dir) - members,
        priority_wallets=members,  # Stage 3 members: polled every run (plan S2)
    ).to_dict()
    result["cohort_v1_members_polled"] = len(members)
    result["status"] = (
        "partial" if not result["windows_succeeded"] or result.get("wallets_with_errors", 0)
        else "succeeded"
    )
    result["cohort_v1_capture"] = _capture_signals(data_dir, offset, run_id)  # before compaction
    result["positions_retention"] = _compact_positions(data_dir)
    result["activity_compaction"] = _compact_activity(data_dir)
    try:
        result["storage_mb"] = _storage_mb(data_dir)
    except OSError as exc:  # a file vanishing mid-walk must not fail collection
        result["storage_mb"] = {"error_type": type(exc).__name__}
    return result


# Each poll appends a full position snapshot per wallet (about 0.27 GB a day for
# 13 wallets on 2026-10-02); readers only need the latest two.
POSITION_SNAPSHOTS_KEPT = 2


# Activity is compacted into gzip segments once the plain file reaches this size
# (jsonl_archive; Stage 3 step 0). About a day of collection at 2026-10-06 rates,
# so segments stay few and the plain tail stays small.
ACTIVITY_COMPACT_MIN_BYTES = 32 * 1024 * 1024


def _capture_signals(data_dir: Path, offset: int, run_id: str) -> dict[str, Any]:
    """Book and fee for this run's cohort-v1 signals (Stage 3 step 2). A failure is
    reported, never raised: collection itself succeeded, and failing it would demand
    manual recovery. Signals it could not record are lost, and the error says so."""
    from .cohort_capture import capture

    try:
        return capture(data_dir, offset, run_id)
    except Exception as exc:  # noqa: BLE001 - any capture fault must leave collection intact
        log.warning("Cohort signal capture failed: %s", type(exc).__name__)
        return {"status": "failed", "error_type": type(exc).__name__}


def _compact_activity(data_dir: Path) -> dict[str, Any]:
    """Move activity rows into compressed segments. A failure is reported, never
    raised: the rows are safe in the plain or pending file either way."""
    from .jsonl_archive import compact, segment_paths

    path = data_dir / "polymarket_activity.jsonl"
    try:
        moved = compact(path, min_bytes=ACTIVITY_COMPACT_MIN_BYTES)
        return {"bytes_moved": moved, "segments": len(segment_paths(path))}
    except OSError as exc:
        log.warning("Activity compaction failed: %s", exc)
        return {"error_type": type(exc).__name__}


def _compact_positions(data_dir: Path) -> dict[str, Any]:
    """Apply position retention. A failure is reported, never raised: a failed
    collect stage would demand manual recovery for data that is already safe."""
    from .storage import compact_position_snapshots

    protected: frozenset[str] = frozenset()
    try:
        state = json.loads((data_dir / "exit_state.json").read_text(encoding="utf-8"))
        if isinstance(state, dict):
            protected = frozenset(str(value) for value in state.values() if value)
    except (OSError, ValueError):
        pass
    try:
        return compact_position_snapshots(data_dir / "polymarket_positions.jsonl",
                                          keep=POSITION_SNAPSHOTS_KEPT, protected_ids=protected)
    except OSError as exc:
        log.warning("Position retention failed: %s", exc)
        return {"error_type": type(exc).__name__}


def run_cycle(data_dir: Path, config: PilotConfig, run_id: str, *,
              execute: StageRunner = _execute_stage,
              now_fn: Callable[[], datetime] = _utcnow,
              monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Worker body. Production callers must use the supervised CLI --run."""
    if not run_id.isalnum():
        raise ValueError("run_id must be alphanumeric")
    control = data_dir / ".lean-pilot"
    with worker_lock(control / "worker.lock"):
        state_path = control / "state.json"
        state = _read_state(state_path)
        previous = state.get("active")
        if previous:
            # Acquiring the worker-owned lock proves its predecessor exited.
            previous_receipt = control / "runs" / previous["run_id"] / "receipt.json"
            _atomic_json(previous_receipt, {**previous, "status": "interrupted",
                                          "reservation_retained": True})
            for name, row in state["stages"].items():
                if row.get("last_status") == "running":
                    row["last_status"] = "interrupted"
                    if name == "collect":
                        state["recovery_required"] = True
                        state["recovery_run_id"] = previous["run_id"]
            state["active"] = None
            _atomic_json(state_path, state)
        now = now_fn()
        if now.tzinfo is None:
            raise ValueError("now_fn must return an aware timestamp")
        now = now.astimezone(UTC)
        plan = plan_cycle(data_dir, config, now=now)
        if not plan["can_run"]:
            status = "recovery_required" if plan["recovery_required"] else (
                "not_due" if not plan["due"] else
                "disk_low" if plan["disk_free_mb"] < config.min_free_disk_mb
                else "budget_exhausted")
            return {"run_id": run_id, "status": status,
                    "plan": plan}
        receipt: dict[str, Any] = {"run_id": run_id, "status": "running",
                                   "started_at": now.isoformat(), "stages": {},
                                   "reserved_seconds": config.cycle_timeout_seconds,
                                   "reservation_days": plan["reservation_days"]}
        receipt_path = control / "runs" / run_id / "receipt.json"
        if receipt_path.exists():
            raise ValueError("Run receipt already exists")
        for day in plan["reservation_days"]:
            state["days"][day] = state["days"].get(day, 0) + config.cycle_timeout_seconds
        state["active"] = receipt
        _atomic_json(state_path, state)  # charge before any collection/scoring work
        _atomic_json(receipt_path, receipt)
        started = monotonic()
        try:
            for stage in plan["due"]:
                stage_state = state["stages"].setdefault(stage, {})
                stage_state.update(last_attempt_at=now_fn().isoformat(), last_status="running")
                _atomic_json(state_path, state)
                result = execute(stage, data_dir, config, run_id)
                status = result.get("status", "succeeded")
                if status not in {"succeeded", "partial"}:
                    raise RuntimeError(f"{stage} returned unsuccessful status: {status}")
                receipt["stages"][stage] = {"status": status, "result": result}
                stage_state["last_status"] = status
                if status == "succeeded":
                    stage_state["last_success_at"] = now_fn().isoformat()
                _atomic_json(state_path, state)
                _atomic_json(receipt_path, receipt)
            receipt["status"] = (
                "partial" if any(r["status"] == "partial" for r in receipt["stages"].values())
                else "succeeded"
            )
        except Exception as exc:
            receipt["status"] = "failed"
            # Upstream exceptions can embed URLs/credentials. Keep raw details
            # in operator logs, not in the durable summary intended for serving.
            receipt["error_type"] = type(exc).__name__
            for name, row in state["stages"].items():
                if row.get("last_status") == "running":
                    row["last_status"] = "failed"
                    if name == "collect":
                        state["recovery_required"] = True
                        state["recovery_run_id"] = run_id
            log.exception("Pilot cycle failed")
        elapsed = max(0, monotonic() - started)
        ended = now_fn().astimezone(UTC)
        for day in plan["reservation_days"]:
            # Charge actual wall duration to every reserved day, conservatively
            # double-counting a midnight crossing instead of undercounting it.
            state["days"][day] += elapsed - config.cycle_timeout_seconds
        state["days"] = {day: seconds for day, seconds in state["days"].items()
                         if day >= (ended - timedelta(days=31)).date().isoformat()}
        receipt.update(finished_at=ended.isoformat(), wall_seconds=elapsed)
        state["active"] = None
        _atomic_json(receipt_path, receipt)
        _atomic_json(state_path, state)
        return receipt


def supervise(command: list[str], log_path: Path, config: PilotConfig) -> dict[str, Any]:
    """Kill an over-budget process tree. Sampled RSS is not a container limit."""
    import psutil

    started = time.monotonic()
    peak = 0
    rss_seconds = 0.0
    previous_sample = started
    cpu_by_process: dict[tuple[int, float], float] = {}
    reason = None
    with log_path.open("x", encoding="utf-8") as output, subprocess.Popen(
        command, stdout=output, stderr=output,
    ) as process:
        monitor = psutil.Process(process.pid)
        try:
            while process.poll() is None:
                rss = 0
                try:
                    members = [monitor, *monitor.children(recursive=True)]
                except psutil.NoSuchProcess:
                    members = []
                for member in members:
                    try:
                        rss += member.memory_info().rss
                        cpu = member.cpu_times()
                        cpu_by_process[(member.pid, member.create_time())] = cpu.user + cpu.system
                    except psutil.NoSuchProcess:
                        pass
                sampled_at = time.monotonic()
                rss_seconds += rss * (sampled_at - previous_sample)
                previous_sample = sampled_at
                peak = max(peak, rss)
                if sampled_at - started >= config.cycle_timeout_seconds:
                    reason = "timeout"
                elif rss > config.rss_limit_mb * 1024**2:
                    reason = "rss_limit"
                elif log_path.stat().st_size > config.log_limit_mb * 1024**2:
                    reason = "log_limit"
                if reason:
                    break
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                try:
                    children = monitor.children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                if process.poll() is None:
                    process.kill()
                psutil.wait_procs(children, timeout=3)
            returncode = process.wait()
    return {"status": "failed" if reason or returncode else "succeeded",
            "stop_reason": reason, "returncode": returncode,
            "wall_seconds": time.monotonic() - started,
            "sampled_peak_rss_bytes": peak, "sampled_rss_gib_seconds": rss_seconds / 1024**3,
            "observed_cpu_seconds": sum(cpu_by_process.values())}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "_worker":
        job = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        data_dir = Path(job["data_dir"])
        os.environ["POLYMARKET_DATA_DIR"] = str(data_dir)
        os.environ["POLYMARKET_WATCHLIST_PATH"] = str(data_dir / "polymarket_wallet_watchlist.txt")
        os.environ["POLYMARKET_WALLET_CONCURRENCY"] = "2"
        os.environ["POLYMARKET_API_RPS"] = "2"
        if os.getenv("DATABASE_URL", "").strip():
            raise ValueError("Lean pilot currently requires JSONL-only storage; unset DATABASE_URL")
        config = PilotConfig(**job["config"])
        os.environ["METADATA_BACKFILL_MAX_CONDITIONS"] = str(config.metadata_conditions_per_cycle)
        os.environ["METADATA_BACKFILL_MAX_REQUESTS"] = str(config.metadata_requests_per_cycle)
        # Second deadline lives inside the worker: a dead supervisor must not
        # leave an orphan consuming resources indefinitely. The worker uses
        # threads only. Abrupt exit intentionally retains its reservation.
        deadline = threading.Timer(config.cycle_timeout_seconds, lambda: os._exit(124))
        deadline.daemon = True
        deadline.start()
        try:
            try:
                result = run_cycle(data_dir, config, job["run_id"])
            except WorkerBusy:
                result = {"run_id": job["run_id"], "status": "busy"}
        finally:
            deadline.cancel()
        _atomic_json(Path(args[2]), result)
        return int(result["status"] in _ALERTING_STATUSES)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="JSON override of PilotConfig defaults")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Read-only plan (default)")
    modes.add_argument("--run", action="store_true", help="Run due stages once, then exit")
    modes.add_argument("--recover", metavar="RUN_ID",
                       help="Repair after the named interrupted collection (pilot_recovery)")
    parsed = parser.parse_args(args)
    config = PilotConfig(**json.loads(parsed.config.read_text()) if parsed.config else {})
    data_dir = parsed.data_dir.resolve()
    if parsed.recover:
        from .pilot_recovery import recover
        outcome = recover(data_dir, parsed.recover)
        log.info("Pilot recovery %s", json.dumps(outcome))
        return int(outcome["status"] not in {"recovered", "nothing_to_recover"})
    if not parsed.run:
        log.info("Pilot plan %s", json.dumps(plan_cycle(data_dir, config)))
        return 0
    run_id = uuid.uuid4().hex
    directory = data_dir / ".lean-pilot" / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    request = directory / "job.json"
    response = directory / "result.json"
    _atomic_json(request, {"data_dir": str(data_dir), "config": asdict(config), "run_id": run_id})
    report = supervise([sys.executable, "-m", "marketsignalos_polymarket.lean_pilot",
                        "_worker", str(request), str(response)], directory / "worker.log", config)
    _atomic_json(directory / "resources.json", report)
    result = json.loads(response.read_text()) if response.exists() else report
    log.info("Pilot result %s; resource report=%s", json.dumps(result), directory / "resources.json")
    return int(report["status"] == "failed" or result["status"] in _ALERTING_STATUSES)


if __name__ == "__main__":
    # stdout, so Railway does not label every line an error.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
