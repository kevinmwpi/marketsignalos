"""
Repair a pilot data directory after an interrupted collection, when an operator asks.

``lean_pilot`` sets ``recovery_required`` when a collect stage is interrupted or fails,
records that run's id as ``recovery_run_id``, and then refuses to run. Blueprint §6
Stage 2 asks for no automatic repair and a documented manual one. This module is that
procedure. The operator names the interrupted run, either with ``--recover RUN_ID`` or
by setting the worker's ``PILOT_RECOVER`` variable to the id the "Pilot result" log
line reports. A variable left set therefore never repairs a later incident by itself.
A state written before run ids were recorded is recovered with the id ``unrecorded``.

The collection writes are not one transaction, so an interruption can leave:

1. **A torn final row in an append-only JSONL store.** The readers skip it, but the
   next append would fuse it with a new row, and that row would be lost. Each store's
   torn tail is cut and saved under the recovery directory. Invalid lines elsewhere in
   a file are counted, not removed.
2. **An unreadable wallet-checkpoint file**, from a kill during a non-atomic write.
   Those writes are atomic now, but older files may be broken. The file is rebuilt from
   the stored activity: each wallet's newest stored timestamp. That is never newer than
   the lost checkpoint, so the next poll refetches, and the dedupe index drops what is
   already stored.
3. **A dedupe index that misses rows written after its last flush.** Those trades would
   be appended again. The index is rebuilt from the stored rows, exactly as the cohort
   stage does after a purge.

``recovery_required`` is cleared only when every step succeeded. A receipt is written
to ``.lean-pilot/recoveries/<run id>.json`` either way.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .closing_lines import _complete_bytes
from .cohort import _rebuild_activity_index, _rows

log = logging.getLogger("marketsignalos.polymarket.pilot_recovery")

ACTIVITY_FILE = "polymarket_activity.jsonl"
CHECKPOINTS_FILE = "polymarket_wallet_checkpoints.json"
STORE_DIRS = ("", "entry_prices", "closing_lines")  # where append-only JSONL lives
UNRECORDED = "unrecorded"


def recover(data_dir: Path, run_id: str) -> dict[str, Any]:
    """Repair ``data_dir`` after the interrupted collection ``run_id``.

    Returns the receipt. Its ``status`` is ``recovered``, ``nothing_to_recover``,
    ``refused`` (another run needs recovery, or none is recorded) or ``failed``.
    """
    from .lean_pilot import _atomic_json, _read_state, worker_lock

    control = data_dir / ".lean-pilot"
    with worker_lock(control / "worker.lock"):
        state_path = control / "state.json"
        state = _read_state(state_path)
        if not state.get("recovery_required", False):
            return {"status": "nothing_to_recover", "run_id": run_id}
        recorded = state.get("recovery_run_id") or UNRECORDED
        if run_id != recorded:
            return {"status": "refused", "run_id": run_id, "recovery_run_id": recorded,
                    "reason": "the run named is not the one that needs recovery"}
        if not run_id.isalnum():
            return {"status": "refused", "run_id": run_id, "reason": "invalid run id"}
        saved = control / "recoveries" / run_id
        receipt: dict[str, Any] = {"run_id": run_id,
                                   "started_at": datetime.now(UTC).isoformat()}
        try:
            receipt["torn_tails"] = _cut_torn_tails(data_dir, saved)
            receipt["invalid_lines"] = _count_invalid_lines(data_dir)
            receipt["checkpoints"] = _repair_checkpoints(data_dir, saved)
            if (data_dir / ACTIVITY_FILE).exists():
                _rebuild_activity_index(data_dir / ACTIVITY_FILE)
                receipt["activity_index"] = "rebuilt"
            else:
                receipt["activity_index"] = "no activity store"
        except (OSError, ValueError) as exc:
            receipt.update(status="failed", error_type=type(exc).__name__)
            log.exception("Pilot recovery failed; recovery_required stays set")
        else:
            receipt["status"] = "recovered"
            state["recovery_required"] = False
            state.pop("recovery_run_id", None)
            state["last_recovery"] = {"run_id": run_id, "at": receipt["started_at"]}
            _atomic_json(state_path, state)
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        _atomic_json(control / "recoveries" / f"{run_id}.json", receipt)
        return receipt


def _jsonl_stores(data_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for sub in STORE_DIRS:
        directory = data_dir / sub if sub else data_dir
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.jsonl")))
    return paths


def _cut_torn_tails(data_dir: Path, saved: Path) -> dict[str, int]:
    """Cut each store's torn final row, keeping the cut bytes; returns bytes cut."""
    cut: dict[str, int] = {}
    for path in _jsonl_stores(data_dir):
        size = path.stat().st_size
        end = _complete_bytes(path) if size else 0
        if end == size:
            continue
        name = path.relative_to(data_dir).as_posix()
        with path.open("rb") as handle:
            handle.seek(end)
            tail = handle.read()
        saved.mkdir(parents=True, exist_ok=True)
        (saved / (name.replace("/", "__") + ".torn")).write_bytes(tail)
        with path.open("rb+") as handle:
            handle.truncate(end)
        cut[name] = len(tail)
    return cut


def _count_invalid_lines(data_dir: Path) -> dict[str, int]:
    """Lines that are not JSON objects, by store. Readers skip them; reported only."""
    counts: dict[str, int] = {}
    for path in _jsonl_stores(data_dir):
        bad = 0
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(row, dict):
                    bad += 1
        if bad:
            counts[path.relative_to(data_dir).as_posix()] = bad
    return counts


def _repair_checkpoints(data_dir: Path, saved: Path) -> str:
    path = data_dir / CHECKPOINTS_FILE
    if not path.exists():
        return "absent"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return "valid"
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    saved.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, saved / f"{CHECKPOINTS_FILE}.broken")
    newest: dict[str, int] = {}
    for row in _rows(data_dir / ACTIVITY_FILE):
        wallet = str(row.get("proxy_wallet", "")).lower()
        ts = row.get("timestamp")
        if wallet and isinstance(ts, int) and not isinstance(ts, bool):
            newest[wallet] = max(newest.get(wallet, ts), ts)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(newest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return f"rebuilt from activity for {len(newest)} wallets"
