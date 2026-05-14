from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("marketsignalos.api.ingestor")
router = APIRouter(prefix="/ingestor", tags=["ingestor"])

# In-memory state — adequate for a single-instance personal tool.
_LOG_TAIL_MAXLEN = 120
_lock = threading.Lock()
_state: dict[str, object] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
    "last_error": None,
    "log_tail": [],
    "last_summary": None,
}
# Per-run log buffer. Reset at run start; snapshotted into _state["log_tail"] at end.
_log_buffer: deque[str] = deque(maxlen=_LOG_TAIL_MAXLEN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestorStatus(BaseModel):
    running: bool
    last_started_at: str | None
    last_finished_at: str | None
    last_exit_code: int | None
    last_error: str | None
    log_tail: list[str]
    last_summary: dict[str, Any] | None


class _BufferHandler(logging.Handler):
    """Captures log records during a run into a shared deque."""

    def __init__(self, buffer: deque[str]) -> None:
        super().__init__()
        self._buffer = buffer
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:
            self.handleError(record)


_PREV_LEVEL: int | None = None


def _attach_log_capture() -> _BufferHandler:
    global _PREV_LEVEL
    handler = _BufferHandler(_log_buffer)
    handler.setLevel(logging.INFO)
    logger = logging.getLogger("marketsignalos")
    # Ensure INFO records actually propagate to our handler. The Polymarket
    # pipeline calls basicConfig(INFO) on import, but the API may run before
    # that import has triggered (and tests stub the import entirely).
    _PREV_LEVEL = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def _detach_log_capture(handler: _BufferHandler) -> None:
    global _PREV_LEVEL
    logger = logging.getLogger("marketsignalos")
    logger.removeHandler(handler)
    if _PREV_LEVEL is not None:
        logger.setLevel(_PREV_LEVEL)
        _PREV_LEVEL = None


def _summarize_failure(exit_code: int, run_error: str | None) -> str:
    if run_error:
        return run_error
    for line in reversed(_log_buffer):
        if " ERROR " in line or " CRITICAL " in line:
            return line
    for line in reversed(_log_buffer):
        if " WARNING " in line:
            return line
    return f"Ingestor exited with code {exit_code} (see log_tail for details)"


def _run_ingestor_sync() -> None:
    """Runs the Polymarket pipeline in a thread-pool worker.

    The pipeline is fully unauthenticated (both Polymarket public APIs and
    Kalshi's public /markets endpoint), so this needs zero env-var setup.
    The legacy Kalshi user-history runner is intentionally NOT invoked
    here — Kalshi hides per-user history publicly, so that pipeline
    cannot reach the product's goal (see docs/0002-cross-exchange-decision.md).
    """
    handler = _attach_log_capture()
    summary: dict[str, Any] | None = None
    run_error: str | None = None
    exit_code = 1
    try:
        try:
            from marketsignalos_polymarket.runner import run_pipeline  # noqa: PLC0415
        except ImportError as exc:
            log.error("polymarket pipeline not importable: %s", exc)
            with _lock:
                _state.update(
                    running=False,
                    last_finished_at=_now(),
                    last_exit_code=1,
                    last_error=f"Polymarket pipeline not importable: {exc}",
                    log_tail=list(_log_buffer),
                    last_summary=None,
                )
            return

        try:
            result = run_pipeline()
            summary = result.to_dict()
            exit_code = 0
            log.info("ingestor run finished exit_code=0")
        except Exception as exc:
            log.exception("polymarket pipeline raised")
            run_error = f"{type(exc).__name__}: {exc}"[:500]

        with _lock:
            _state["running"] = False
            _state["last_finished_at"] = _now()
            _state["last_exit_code"] = exit_code
            if exit_code == 0 and run_error is None:
                _state["last_error"] = None
            else:
                _state["last_error"] = _summarize_failure(exit_code, run_error)
            _state["log_tail"] = list(_log_buffer)
            _state["last_summary"] = summary
    finally:
        _detach_log_capture(handler)


@router.get("/status", response_model=IngestorStatus)
def get_ingestor_status() -> IngestorStatus:
    with _lock:
        return IngestorStatus(
            running=bool(_state["running"]),
            last_started_at=cast("str | None", _state["last_started_at"]),
            last_finished_at=cast("str | None", _state["last_finished_at"]),
            last_exit_code=cast("int | None", _state["last_exit_code"]),
            last_error=cast("str | None", _state["last_error"]),
            log_tail=list(cast("list[str]", _state["log_tail"])),
            last_summary=cast("dict[str, Any] | None", _state["last_summary"]),
        )


@router.post("/run", status_code=202)
async def trigger_ingestor_run() -> JSONResponse:
    """Trigger one pass of the Polymarket → Kalshi pipeline.

    This is unauthenticated (uses only public APIs) so it needs no
    pre-flight config check — the only failure mode is "already running",
    which returns 409.
    """
    try:
        from marketsignalos_polymarket.runner import run_pipeline  # noqa: F401, PLC0415
    except ImportError as exc:
        log.error("polymarket pipeline not importable: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc

    with _lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="Ingestion already running")
        _state["running"] = True
        started_at = _now()
        _state["last_started_at"] = started_at
        _state["last_finished_at"] = None
        _state["last_exit_code"] = None
        _state["last_error"] = None
        _state["last_summary"] = None
        _log_buffer.clear()
        _state["log_tail"] = []

    log.info("ingestor run triggered started_at=%s", started_at)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_ingestor_sync)
    return JSONResponse(
        status_code=202,
        content={"status": "started", "started_at": started_at},
    )
