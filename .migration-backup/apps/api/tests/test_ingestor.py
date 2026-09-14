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
        kind=None,
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
        "kind": None,
        "last_started_at": None,
        "last_finished_at": None,
        "last_exit_code": None,
        "last_error": None,
        "log_tail": [],
        "last_summary": None,
        "progress": None,
        "schedule": None,
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
    assert status["kind"] == "shallow"
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


def test_run_deep_starts_deep_pipeline_and_records_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /ingestor/run/deep kicks off the deep pipeline and surfaces its
    extended counter set in last_summary."""
    deep_summary = {
        "windows_attempted": [],
        "windows_succeeded": [],
        "leaderboard_entries": 1500,
        "wallets_seeded": 800,
        "activity_records": 50_000,
        "positions": 1200,
        "wallet_values": 800,
        "markets_written": 500,
        "markets_backfilled": 20,
        "enrichment_wallets": 600,
        "kalshi_markets": 800,
        "market_links": 18,
        "discovered_this_run": 7000,
        "deep_slices_attempted": 400,
        "deep_slices_succeeded": 396,
        "active_wallets": 6800,
        "archived_wallets": 150,
        "pinned_wallets": 5,
        "pruned_this_run": 12,
    }

    def _fake_deep_run() -> None:
        ingestor_route.log.info("deep_pipeline complete %s", deep_summary)
        with ingestor_route._lock:
            ingestor_route._state["running"] = False
            ingestor_route._state["last_finished_at"] = ingestor_route._now()
            ingestor_route._state["last_exit_code"] = 0
            ingestor_route._state["last_error"] = None
            ingestor_route._state["log_tail"] = list(ingestor_route._log_buffer)
            ingestor_route._state["last_summary"] = deep_summary

    _wire_immediate_deep_executor(monkeypatch, _fake_deep_run)

    client = TestClient(app)
    resp = client.post("/ingestor/run/deep")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "started"
    assert body["kind"] == "deep"

    status = client.get("/ingestor/status").json()
    assert status["kind"] == "deep"
    assert status["last_exit_code"] == 0
    assert status["last_summary"] == deep_summary
    assert status["last_summary"]["discovered_this_run"] == 7000
    assert status["last_summary"]["archived_wallets"] == 150


def test_run_deep_returns_409_when_already_running() -> None:
    """Shallow and deep runs share the running flag — only one at a time."""
    ingestor_route._state["running"] = True
    client = TestClient(app)
    resp = client.post("/ingestor/run/deep")
    assert resp.status_code == 409


def test_prune_wallets_returns_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /ingestor/prune-wallets runs synchronously and returns the counts
    from run_prune_wallets()."""
    captured: dict[str, Any] = {}

    def _fake_prune(*, dormant_days: int = 90, dry_run: bool = False) -> dict[str, int]:
        captured["dormant_days"] = dormant_days
        captured["dry_run"] = dry_run
        return {
            "pruned_this_run": 7,
            "active_wallets": 1234,
            "archived_wallets": 567,
            "pinned_wallets": 3,
        }

    # Patch the symbol on the runner module — the route does a late import.
    import marketsignalos_polymarket.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_prune_wallets", _fake_prune)

    client = TestClient(app)
    resp = client.post("/ingestor/prune-wallets?dormant_days=120&dry_run=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["pruned_this_run"] == 7
    assert body["archived_wallets"] == 567
    assert captured == {"dormant_days": 120, "dry_run": True}


def test_prune_wallets_returns_409_when_pipeline_running() -> None:
    """Prune mustn't race with a pipeline rewriting the same review-state file."""
    ingestor_route._state["running"] = True
    client = TestClient(app)
    resp = client.post("/ingestor/prune-wallets")
    assert resp.status_code == 409


def _wire_immediate_deep_executor(
    monkeypatch: pytest.MonkeyPatch, fake_run: Any,
) -> None:
    """Mirror of _wire_immediate_executor for the deep pipeline path."""
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

    monkeypatch.setattr(ingestor_route, "_run_deep_ingestor_sync", fake_run)
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: _ImmediateLoop())


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


# ── Manual watchlist endpoints ───────────────────────────────────────────────

def test_watchlist_add_list_and_idempotency(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    client = TestClient(app)
    address = "0x" + "b" * 40

    resp = client.post("/ingestor/watchlist", json={"address": address})
    assert resp.status_code == 201
    body = resp.json()
    assert body["added"] is True
    assert body["wallet"] == address
    assert body["review_status"] == "pinned"

    # Re-adding returns 200 and does not duplicate.
    resp = client.post("/ingestor/watchlist", json={"address": address})
    assert resp.status_code == 200
    assert resp.json()["added"] is False

    listed = client.get("/ingestor/watchlist").json()
    assert listed == {"count": 1, "addresses": [address]}


def test_watchlist_rejects_invalid_address_with_422(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    resp = TestClient(app).post(
        "/ingestor/watchlist", json={"address": "not-a-wallet"}
    )
    assert resp.status_code == 422
    assert "0x-prefixed" in resp.json()["detail"]


def test_watchlist_add_returns_409_while_pipeline_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline rewrites the watchlist mid-run; a concurrent manual add
    would race that read-modify-write."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    ingestor_route._state["running"] = True
    resp = TestClient(app).post(
        "/ingestor/watchlist", json={"address": "0x" + "c" * 40}
    )
    assert resp.status_code == 409
