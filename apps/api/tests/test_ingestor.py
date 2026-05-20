from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.api.routes import ingestor as ingestor_route
from marketsignalos_api.main import app


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    ingestor_route._log_buffer.clear()
    ingestor_route._state.update(
        running=False,
        last_started_at=None,
        last_finished_at=None,
        last_exit_code=None,
        last_error=None,
        log_tail=[],
        last_summary=None,
        progress=None,
    )


def test_status_default_state_includes_log_tail_and_summary() -> None:
    client = TestClient(app)
    resp = client.get("/ingestor/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "last_exit_code": None,
        "last_error": None,
        "log_tail": [],
        "last_summary": None,
        "progress": None,
    }


def test_set_progress_snapshots_payload_and_status_returns_it() -> None:
    """The progress callback must be safe to call from the executor thread —
    snapshotting prevents the caller from mutating what /status reads."""
    payload: dict[str, object] = {
        "stage": "wallets", "current": 12, "total": 50, "wallet": "0xabc",
    }
    ingestor_route._set_progress(payload)
    # Caller-side mutation must not bleed into stored state.
    payload["current"] = 999

    client = TestClient(app)
    body = client.get("/ingestor/status").json()
    assert body["progress"] == {
        "stage": "wallets", "current": 12, "total": 50, "wallet": "0xabc",
    }


def test_run_starts_pipeline_and_records_success_with_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful run: pipeline returns a PipelineResult, summary is exposed."""

    fake_summary = {
        "windows_attempted": ["day", "week", "month", "all"],
        "windows_succeeded": ["all"],
        "leaderboard_entries": 50,
        "wallets_seeded": 40,
        "activity_records": 1200,
        "positions": 80,
        "wallet_values": 40,
        "markets_written": 500,
        "markets_backfilled": 20,
        "enrichment_wallets": 32,
        "kalshi_markets": 800,
        "market_links": 18,
    }

    def _fake_run() -> None:
        ingestor_route.log.info("pipeline complete %s", fake_summary)
        with ingestor_route._lock:
            ingestor_route._state["running"] = False
            ingestor_route._state["last_finished_at"] = ingestor_route._now()
            ingestor_route._state["last_exit_code"] = 0
            ingestor_route._state["last_error"] = None
            ingestor_route._state["log_tail"] = list(ingestor_route._log_buffer)
            ingestor_route._state["last_summary"] = fake_summary

    _wire_immediate_executor(monkeypatch, _fake_run)

    client = TestClient(app)
    resp = client.post("/ingestor/run")
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"

    status = client.get("/ingestor/status").json()
    assert status["running"] is False
    assert status["last_exit_code"] == 0
    assert status["last_error"] is None
    assert status["last_summary"] == fake_summary
    assert any("pipeline complete" in line for line in status["log_tail"])


def test_run_surfaces_pipeline_exception_with_log_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipeline raises mid-run: last_error captures the message, summary stays None."""

    def _fake_run() -> None:
        ingestor_route.log.error("simulated upstream failure: connection refused")
        with ingestor_route._lock:
            ingestor_route._state["running"] = False
            ingestor_route._state["last_finished_at"] = ingestor_route._now()
            ingestor_route._state["last_exit_code"] = 1
            ingestor_route._state["last_error"] = ingestor_route._summarize_failure(1, None)
            ingestor_route._state["log_tail"] = list(ingestor_route._log_buffer)
            ingestor_route._state["last_summary"] = None

    _wire_immediate_executor(monkeypatch, _fake_run)

    client = TestClient(app)
    resp = client.post("/ingestor/run")
    assert resp.status_code == 202

    status = client.get("/ingestor/status").json()
    assert status["running"] is False
    assert status["last_exit_code"] == 1
    assert status["last_error"] is not None
    assert "simulated upstream failure" in status["last_error"]
    assert status["last_summary"] is None
    assert any("simulated upstream failure" in line for line in status["log_tail"])


def test_run_returns_409_when_already_running() -> None:
    ingestor_route._state["running"] = True
    client = TestClient(app)
    resp = client.post("/ingestor/run")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ingestion already running"


def _wire_immediate_executor(
    monkeypatch: pytest.MonkeyPatch, fake_run: Any,
) -> None:
    """Replace asyncio's run_in_executor with an inline call, and intercept
    _run_ingestor_sync with the supplied stub. Mirrors production by also
    attaching/detaching the log capture handler so log_tail reflects what
    the stub emits."""

    def _stub_executor(_executor: Any, fn: Any, *args: Any) -> None:
        handler = ingestor_route._attach_log_capture()
        try:
            fn(*args)
        finally:
            ingestor_route._detach_log_capture(handler)

    import asyncio

    class _ImmediateLoop:
        def run_in_executor(self, executor: Any, fn: Any, *args: Any) -> None:
            _stub_executor(executor, fn, *args)

    monkeypatch.setattr(ingestor_route, "_run_ingestor_sync", fake_run)
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: _ImmediateLoop())
