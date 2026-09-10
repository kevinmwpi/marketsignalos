"""Durable run receipts and a small public status response (no history scans)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import _polymarket_dir

log = logging.getLogger("marketsignalos.platform")
_DATASETS = ("polymarket_wallet_enrichment.jsonl", "polymarket_positions.jsonl",
             "polymarket_markets.jsonl")


def _receipt_path() -> Path:
    return _polymarket_dir() / "platform_run.json"


def load_receipt() -> dict[str, Any]:
    try:
        value = json.loads(_receipt_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_receipt(state: dict[str, object]) -> None:
    """Atomic replacement; never persist private log output or upstream error text."""
    previous = load_receipt()
    receipt = {key: state.get(key) for key in (
        "running", "kind", "last_started_at", "last_finished_at", "last_exit_code",
        "last_summary",
    )}
    receipt["last_success_at"] = previous.get("last_success_at")
    summary = state.get("last_summary")
    partial = isinstance(summary, dict) and bool(summary.get("warning"))
    if not state.get("running") and state.get("last_exit_code") == 0 and not partial:
        receipt["last_success_at"] = state.get("last_finished_at")
    path = _receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def persist_run(state: dict[str, object]) -> None:
    try:
        save_receipt(state)
    except OSError:
        log.exception("could not persist pipeline receipt")


def restore_run() -> dict[str, Any]:
    receipt = load_receipt()
    if receipt.get("running"):
        receipt.update(
            running=False, last_exit_code=1,
            last_finished_at=datetime.now(timezone.utc).isoformat(),
        )
        persist_run(receipt)
    return receipt


def public_status() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    receipt = load_receipt()
    try:
        threshold = max(1, int(os.getenv("DATA_STALE_AFTER_MINUTES", "120")))
    except ValueError:
        threshold = 120
    ages: list[float] = []
    missing: list[str] = []
    for filename in _DATASETS:
        try:
            stat = (_polymarket_dir() / filename).stat()
            if stat.st_size == 0:
                missing.append(filename)
            else:
                ages.append(max(0, now.timestamp() - stat.st_mtime))
        except OSError:
            missing.append(filename)
    last_success = receipt.get("last_success_at")
    try:
        success_time = datetime.fromisoformat(str(last_success))
        if success_time.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        run_age = max(0, (now - success_time).total_seconds())
    except (TypeError, ValueError):
        run_age = None
    summary = receipt.get("last_summary") or {}
    partial = isinstance(summary, dict) and bool(summary.get("warning"))
    if missing or run_age is None:
        freshness = "unavailable"
    elif max([run_age, *ages]) > threshold * 60:
        freshness = "stale"
    elif receipt.get("last_exit_code") not in (None, 0) or partial:
        freshness = "degraded"
    elif receipt.get("running"):
        freshness = "updating"
    else:
        freshness = "recent"
    return {
        "checked_at": now.isoformat(),
        "freshness": freshness,
        "last_success_at": last_success,
        "oldest_dataset_age_seconds": round(max(ages)) if ages else None,
        "stale_after_minutes": threshold,
        "missing_datasets": missing,
        "ingestion_running": bool(receipt.get("running", False)),
        "last_run_partial": partial,
        "storage_mode": "jsonl_with_postgres_mirror" if os.getenv("DATABASE_URL") else "jsonl",
        "coverage": "sampled_wallets",
        "score_interpretation": "Bayesian historical edge estimate; not proof of insider activity",
    }
