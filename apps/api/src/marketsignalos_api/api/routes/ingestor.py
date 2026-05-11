from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger("marketsignalos.api.ingestor")
router = APIRouter(prefix="/ingestor", tags=["ingestor"])

# In-memory state — adequate for a single-instance personal tool.
_lock = threading.Lock()
_state: dict[str, object] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestorStatus(BaseModel):
    running: bool
    last_started_at: str | None
    last_finished_at: str | None
    last_exit_code: int | None
    last_error: str | None


def _run_ingestor_sync() -> None:
    """Runs a single ingestor pass in a thread-pool worker."""
    try:
        from marketsignalos_ingestor import runner  # noqa: PLC0415
    except ImportError as exc:
        with _lock:
            _state.update(running=False, last_finished_at=_now(), last_exit_code=1, last_error=str(exc))
        log.error("ingestor package not importable: %s", exc)
        return

    # Force single-pass regardless of the INGEST_CONTINUOUS env var.
    prev = os.environ.get("INGEST_CONTINUOUS")
    os.environ["INGEST_CONTINUOUS"] = "false"
    exit_code = 1
    try:
        exit_code = runner.main()
        log.info("ingestor run finished exit_code=%d", exit_code)
    except Exception as exc:
        log.exception("ingestor run raised: %s", exc)
        with _lock:
            _state["last_error"] = str(exc)[:500]
    finally:
        if prev is None:
            os.environ.pop("INGEST_CONTINUOUS", None)
        else:
            os.environ["INGEST_CONTINUOUS"] = prev

    with _lock:
        _state["running"] = False
        _state["last_finished_at"] = _now()
        _state["last_exit_code"] = exit_code
        if exit_code == 0:
            _state["last_error"] = None


@router.get("/status", response_model=IngestorStatus)
def get_ingestor_status() -> IngestorStatus:
    with _lock:
        return IngestorStatus(**_state)  # type: ignore[arg-type]


@router.post("/run", status_code=202)
async def trigger_ingestor_run() -> dict[str, object]:
    with _lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="Ingestion already running")
        _state["running"] = True
        started_at = _now()
        _state["last_started_at"] = started_at
        _state["last_finished_at"] = None
        _state["last_exit_code"] = None
        _state["last_error"] = None

    log.info("ingestor run triggered started_at=%s", started_at)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_ingestor_sync)
    return {"status": "started", "started_at": started_at}
