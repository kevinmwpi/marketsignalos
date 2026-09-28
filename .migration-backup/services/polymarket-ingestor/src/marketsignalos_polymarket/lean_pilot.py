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

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.daily_runtime_seconds < self.cycle_timeout_seconds:
            raise ValueError("daily_runtime_seconds must cover one complete cycle reservation")
        if self.cycle_timeout_seconds > 3600:
            raise ValueError("Pilot cycles must be at most one hour")
        if self.wallet_batch_size > 100 or self.leaderboard_limit > 100:
            raise ValueError("Pilot wallet batch and leaderboard limits must be at most 100")


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
        if stage not in {"collect", "score"} or not isinstance(row, dict):
            raise ValueError("Invalid stage state")
        for key in ("last_attempt_at", "last_success_at"):
            if row.get(key) is not None:
                _timestamp(row[key])
    active = state.get("active")
    if type(state.get("recovery_required", False)) is not bool:
        raise ValueError("Invalid recovery state")
    if active is not None:
        if not isinstance(active, dict) or not isinstance(active.get("run_id"), str):
            raise ValueError("Invalid active run")
        if not active["run_id"].isalnum():
            raise ValueError("Invalid active run identifier")
    return state


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
    for name, interval in (("collect", config.collect_every_seconds),
                           ("score", config.score_every_seconds)):
        row = state["stages"].get(name, {})
        attempted = row.get("last_attempt_at")
        next_due = _timestamp(attempted) + timedelta(seconds=interval) if attempted else now
        if now >= next_due:
            due.append(name)
        stages[name] = {**row, "next_due_at": next_due.isoformat()}
    # Reserve in both days for a cycle that could cross UTC midnight. This is
    # deliberately conservative; successful completion refunds unused time.
    days = sorted({now.date().isoformat(),
                   (now + timedelta(seconds=config.cycle_timeout_seconds)).date().isoformat()})
    remaining = min(config.daily_runtime_seconds - state["days"].get(day, 0) for day in days)
    return {"schema_version": SCHEMA_VERSION, "budget_target_usd_month": 15,
            "budget_is_billing_cap": False, "config": asdict(config), "due": due,
            "stages": stages, "reservation_days": days,
            "runtime_remaining_seconds": max(0, remaining),
            "recovery_required": state.get("recovery_required", False),
            "can_run": bool(due) and remaining >= config.cycle_timeout_seconds
            and not state.get("recovery_required", False)}


StageRunner = Callable[[str, Path, PilotConfig, str], dict[str, Any]]


def _execute_stage(stage: str, data_dir: Path, config: PilotConfig,
                   run_id: str) -> dict[str, Any]:
    if stage == "score":
        from .score_snapshot import score_snapshot
        return score_snapshot(data_dir, data_dir / "score-snapshots", run_id)
    from .runner import run_pipeline
    result = run_pipeline(
        windows=["day"], leaderboard_limit=config.leaderboard_limit,
        wallet_batch_size=config.wallet_batch_size, skip_enrichment=True,
        max_pages_per_wallet=2, market_pages=1, refresh_reference=False,
        max_activity_requests_per_wallet=config.activity_requests_per_wallet,
    ).to_dict()
    result["status"] = (
        "partial" if not result["windows_succeeded"] or result.get("wallets_with_errors", 0)
        else "succeeded"
    )
    return result


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
            state["active"] = None
            _atomic_json(state_path, state)
        now = now_fn()
        if now.tzinfo is None:
            raise ValueError("now_fn must return an aware timestamp")
        now = now.astimezone(UTC)
        plan = plan_cycle(data_dir, config, now=now)
        if not plan["can_run"]:
            status = "recovery_required" if plan["recovery_required"] else (
                "not_due" if not plan["due"] else "budget_exhausted")
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
        return int(result["status"] in {"failed", "recovery_required"})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="JSON override of PilotConfig defaults")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Read-only plan (default)")
    modes.add_argument("--run", action="store_true", help="Run due stages once, then exit")
    parsed = parser.parse_args(args)
    config = PilotConfig(**json.loads(parsed.config.read_text()) if parsed.config else {})
    data_dir = parsed.data_dir.resolve()
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
    return int(report["status"] == "failed" or result["status"] in {"failed", "recovery_required"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
