from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from marketsignalos_api.services.ingest_scheduler import get_schedule_status

log = logging.getLogger("marketsignalos.api.ingestor")
router = APIRouter(prefix="/ingestor", tags=["ingestor"])

# In-memory state — adequate for a single-instance personal tool.
_LOG_TAIL_MAXLEN = 120
_lock = threading.Lock()
_state: dict[str, object] = {
    "running": False,
    "kind": None,  # "shallow" | "deep" — which run is/was most recently active
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
    "last_error": None,
    "log_tail": [],
    "last_summary": None,
    "progress": None,
}
# Per-run log buffer. Reset at run start; snapshotted into _state["log_tail"] at end.
_log_buffer: deque[str] = deque(maxlen=_LOG_TAIL_MAXLEN)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class IngestorStatus(BaseModel):
    running: bool
    kind: str | None
    last_started_at: str | None
    last_finished_at: str | None
    last_exit_code: int | None
    last_error: str | None
    log_tail: list[str]
    last_summary: dict[str, Any] | None
    progress: dict[str, Any] | None
    # Background-scheduler state; None when INGEST_EVERY_MINUTES is unset.
    schedule: dict[str, Any] | None


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
        except Exception:  # noqa: BLE001 — required Handler.emit contract
            # A logging handler must never let an exception escape into the
            # code that logged; the stdlib's own handlers catch bare Exception
            # here and route it to handleError for exactly this reason.
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


def _set_progress(payload: dict[str, Any]) -> None:
    """Thread-safe progress writer passed to the pipeline.

    The pipeline runs in an executor thread; the /status endpoint reads from
    the event loop. We snapshot the payload (shallow copy) so the caller
    can't mutate the value the status endpoint will return.
    """
    snapshot = dict(payload)
    with _lock:
        _state["progress"] = snapshot


def _execute_pipeline_sync(pipeline_callable: Any) -> None:
    """Run any PolymarketPipeline callable in a worker thread, capturing logs
    and persisting state. Shared harness for shallow + deep runs."""
    handler = _attach_log_capture()
    summary: dict[str, Any] | None = None
    run_error: str | None = None
    exit_code = 1
    try:
        try:
            result = pipeline_callable(progress_cb=_set_progress)
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
            _state["progress"] = None

        # The run rewrote the JSONL stores; drop the memoized skilled-bets so
        # the next dashboard request reflects new data immediately rather than
        # waiting for the input-file fingerprint to change. Best effort — a
        # stale cache is recoverable and self-heals on the next fingerprint
        # change, so a failure here must not abort the status update above.
        try:
            from marketsignalos_api.services.skilled_bets import (
                invalidate_cache,
            )

            invalidate_cache()
        except Exception:
            log.warning("could not invalidate skilled_bets cache", exc_info=True)

        # Post-ingest signal passes, in dependency order:
        #   1. ledger    — record newly surfaced signals + settle resolved ones
        #   2. exits     — diff position snapshots (reads the ledger for
        #                  was_surfaced flags)
        #   3. notifier  — webhook-deliver whatever the first two produced
        # All run after cache invalidation so they see fresh data. Each is
        # best effort — a missed pass self-heals on the next run or via its
        # /signals/*/refresh|run endpoint.
        if exit_code == 0:
            try:
                from marketsignalos_api.services.signal_ledger import (
                    update_signal_ledger,
                )

                update_signal_ledger()
            except Exception:
                log.warning("could not update signal ledger", exc_info=True)
            try:
                from marketsignalos_api.services.exit_signals import (
                    update_exit_signals,
                )

                update_exit_signals()
            except Exception:
                log.warning("could not update exit signals", exc_info=True)
            try:
                from marketsignalos_api.services.notifications import (
                    run_notification_pass,
                )

                run_notification_pass()
            except Exception:
                log.warning("could not run notification pass", exc_info=True)
    finally:
        _detach_log_capture(handler)


def _record_import_failure(exc: ImportError) -> None:
    """Surface a pipeline-module import failure into the status state so the
    UI shows what went wrong instead of just spinning."""
    log.error("polymarket pipeline not importable: %s", exc)
    with _lock:
        _state.update(
            running=False,
            last_finished_at=_now(),
            last_exit_code=1,
            last_error=f"Polymarket pipeline not importable: {exc}",
            log_tail=list(_log_buffer),
            last_summary=None,
            progress=None,
        )


def _run_ingestor_sync() -> None:
    """Runs the shallow Polymarket pipeline (the dashboard's 'Run ingest' path).

    The pipeline is fully unauthenticated (both Polymarket public APIs and
    Kalshi's public /markets endpoint), so this needs zero env-var setup.
    The legacy Kalshi user-history runner is intentionally NOT invoked
    here — Kalshi hides per-user history publicly, so that pipeline
    cannot reach the product's goal (identifying skilled wallets, see
    docs/0002-cross-exchange-decision.md).
    """
    try:
        from marketsignalos_polymarket.runner import run_pipeline
    except ImportError as exc:
        _record_import_failure(exc)
        return
    _execute_pipeline_sync(run_pipeline)


def _run_deep_ingestor_sync() -> None:
    """Runs the deep review pipeline (categorized leaderboard sweep + prune)."""
    try:
        from marketsignalos_polymarket.runner import run_deep_pipeline
    except ImportError as exc:
        _record_import_failure(exc)
        return
    _execute_pipeline_sync(run_deep_pipeline)


def start_pipeline_run(kind: str) -> str | None:
    """Dispatch one pipeline run ("shallow" | "deep") on the event loop's
    default executor.

    Shared by the HTTP handlers and the background ingest scheduler so a
    scheduled run is indistinguishable from a clicked one: same single
    running flag, same log capture, same post-ingest hook chain. Returns
    the started_at timestamp, or None when a run of either kind is already
    in flight.
    """
    runner = _run_ingestor_sync if kind == "shallow" else _run_deep_ingestor_sync
    with _lock:
        if _state["running"]:
            return None
        _state["running"] = True
        _state["kind"] = kind
        started_at = _now()
        _state["last_started_at"] = started_at
        _state["last_finished_at"] = None
        _state["last_exit_code"] = None
        _state["last_error"] = None
        _state["last_summary"] = None
        _state["progress"] = None
        _log_buffer.clear()
        _state["log_tail"] = []

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, runner)
    return started_at


@router.get("/status", response_model=IngestorStatus)
def get_ingestor_status() -> IngestorStatus:
    with _lock:
        return IngestorStatus(
            running=bool(_state["running"]),
            kind=cast("str | None", _state["kind"]),
            last_started_at=cast("str | None", _state["last_started_at"]),
            last_finished_at=cast("str | None", _state["last_finished_at"]),
            last_exit_code=cast("int | None", _state["last_exit_code"]),
            last_error=cast("str | None", _state["last_error"]),
            log_tail=list(cast("list[str]", _state["log_tail"])),
            last_summary=cast("dict[str, Any] | None", _state["last_summary"]),
            progress=cast("dict[str, Any] | None", _state["progress"]),
            schedule=get_schedule_status(),
        )


@router.post("/run", status_code=202)
async def trigger_ingestor_run() -> JSONResponse:
    """Trigger one pass of the Polymarket → Kalshi pipeline.

    This is unauthenticated (uses only public APIs) so it needs no
    pre-flight config check — the only failure mode is "already running",
    which returns 409.
    """
    try:
        from marketsignalos_polymarket.runner import run_pipeline  # noqa: F401
    except ImportError as exc:
        log.error("polymarket pipeline not importable: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc

    started_at = start_pipeline_run("shallow")
    if started_at is None:
        raise HTTPException(status_code=409, detail="Ingestion already running")

    log.info("ingestor run triggered started_at=%s", started_at)
    return JSONResponse(
        status_code=202,
        content={"status": "started", "started_at": started_at},
    )


@router.post("/run/deep", status_code=202)
async def trigger_deep_ingestor_run() -> JSONResponse:
    """Trigger a deep review pass: categorized leaderboard sweep + prune +
    batched hydration of active+pinned wallets.

    Shares the single "currently running" flag with /ingestor/run, so the two
    can't execute concurrently — returns 409 if a run of either kind is
    already in flight.
    """
    try:
        from marketsignalos_polymarket.runner import run_deep_pipeline  # noqa: F401
    except ImportError as exc:
        log.error("polymarket pipeline not importable: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc

    started_at = start_pipeline_run("deep")
    if started_at is None:
        raise HTTPException(status_code=409, detail="Ingestion already running")

    log.info("deep ingestor run triggered started_at=%s", started_at)
    return JSONResponse(
        status_code=202,
        content={"status": "started", "started_at": started_at, "kind": "deep"},
    )


class WatchlistAddRequest(BaseModel):
    address: str


@router.get("/watchlist")
def get_watchlist() -> dict[str, Any]:
    """Current wallet watchlist (manual + auto-seeded, merged)."""
    try:
        from marketsignalos_polymarket.runner import run_list_watchlist
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc
    addresses = run_list_watchlist()
    return {"count": len(addresses), "addresses": addresses}


@router.post("/watchlist", status_code=201)
def add_watchlist_wallet(body: WatchlistAddRequest) -> JSONResponse:
    """Manually add a wallet to the watchlist and pin it so it can never be
    archived. Idempotent — re-adding an existing wallet returns 200. The
    wallet is hydrated (activity, positions, enrichment) on the next
    pipeline run. Returns 409 while a pipeline run is in flight to avoid
    racing its watchlist rewrite."""
    try:
        from marketsignalos_polymarket.runner import (
            run_add_watchlist_wallet,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc

    with _lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="Ingestion already running")

    try:
        result = run_add_watchlist_wallet(body.address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        status_code=201 if result["added"] else 200,
        content={"status": "ok", **result},
    )


@router.post("/prune-wallets")
def trigger_prune_wallets(
    dormant_days: int = 90, dry_run: bool = False,
) -> JSONResponse:
    """Apply dormancy archival to review-state. Synchronous (the work is
    cheap — reads two JSONL files, mutates one). Returns 409 if a pipeline
    is currently running to avoid clobbering its concurrent rewrite of
    polymarket_wallet_review_state.jsonl.
    """
    try:
        from marketsignalos_polymarket.runner import run_prune_wallets
    except ImportError as exc:
        log.error("polymarket pipeline not importable: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Polymarket pipeline not importable: {exc}",
        ) from exc

    with _lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="Ingestion already running")

    counts = run_prune_wallets(dormant_days=dormant_days, dry_run=dry_run)
    return JSONResponse(status_code=200, content={"status": "ok", **counts})
