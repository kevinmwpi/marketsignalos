from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.cloud_config import validate_cloud_config
from marketsignalos_api.api.routes import ingestor
from marketsignalos_api.main import create_app
from marketsignalos_api.services.platform_status import (
    load_receipt, public_status, restore_run, save_receipt,
)


@pytest.mark.parametrize("path", [
    "/ingestor/run", "/ingestor/run/deep", "/signals/ledger/refresh",
    "/signals/exits/refresh", "/signals/notifications/run",
])
def test_public_cannot_mutate(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_ADMIN")
    client = TestClient(create_app())
    assert client.post(path).status_code == 503
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_operator_logs_private_public_status_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    client = TestClient(create_app())
    for path in ("/ingestor/status", "/signals/notifications/status", "/metrics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/platform/status").status_code == 200


def test_authorized_operator_can_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    dispatched: list[str] = []

    def dispatch(kind: str) -> str:
        dispatched.append(kind)
        return "2026-09-08T00:00:00+00:00"

    monkeypatch.setattr(ingestor, "start_pipeline_run", dispatch)
    client = TestClient(create_app())
    assert client.post("/ingestor/run/deep", headers={
        "Authorization": "Bearer secret",
    }).status_code == 202
    assert dispatched == ["deep"]


def test_run_lock_includes_post_ingest_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from marketsignalos_api.services import exit_signals, notifications, signal_ledger

    seen: list[bool] = []

    def hook() -> None:
        seen.append(bool(ingestor._state["running"]))
        assert ingestor.start_pipeline_run("shallow") is None

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(signal_ledger, "update_signal_ledger", hook)
    monkeypatch.setattr(exit_signals, "update_exit_signals", hook)
    monkeypatch.setattr(notifications, "run_notification_pass", hook)
    monkeypatch.setattr(ingestor, "_state", {"running": True})
    ingestor._execute_pipeline_sync(lambda **_: Result())
    assert seen == [True, True, True]
    assert ingestor._state["running"] is False
    assert load_receipt()["last_exit_code"] == 0


def test_deep_workload_configuration_reaches_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from marketsignalos_polymarket import runner

    options: dict[str, Any] = {}

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {}

    def run(**kwargs: Any) -> Result:
        options.update(kwargs)
        return Result()

    monkeypatch.setattr(runner, "run_deep_pipeline", run)
    monkeypatch.setattr(ingestor, "_execute_pipeline_sync", lambda fn: fn())
    monkeypatch.setenv("INGEST_DEEP_WALLET_BATCH_SIZE", "12")
    ingestor._run_deep_ingestor_sync()
    assert options["wallet_batch_size"] == 12
    assert options["recent_trader_max_pages"] == 20
    monkeypatch.setenv("INGEST_DEEP_WALLET_BATCH_SIZE", "0")
    with pytest.raises(ValueError, match="positive integer"):
        ingestor._run_deep_ingestor_sync()


def test_production_ignores_local_auth_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    assert TestClient(create_app()).post("/ingestor/run").status_code == 503


def _successful_run(tmp_path: Path) -> None:
    for filename in ("polymarket_wallet_enrichment.jsonl", "polymarket_positions.jsonl",
                     "polymarket_markets.jsonl"):
        (tmp_path / filename).write_text('{}\n', encoding="utf-8")
    save_receipt({
        "running": False, "last_exit_code": 0,
        "last_finished_at": datetime.now(timezone.utc).isoformat(),
        "last_summary": {}, "log_tail": ["private"], "last_error": "secret",
    })


def test_missing_old_and_recent_data(tmp_path: Path) -> None:
    assert public_status()["freshness"] == "unavailable"
    _successful_run(tmp_path)
    assert public_status()["freshness"] == "recent"
    old = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    os.utime(tmp_path / "polymarket_positions.jsonl", (old, old))
    assert public_status()["freshness"] == "stale"
    assert "private" not in str(load_receipt())
    assert "secret" not in str(load_receipt())


def test_partial_and_failed_runs_preserve_last_success(tmp_path: Path) -> None:
    _successful_run(tmp_path)
    success = load_receipt()["last_success_at"]
    save_receipt({"running": False, "last_exit_code": 0,
                  "last_summary": {"warning": "partial discovery"},
                  "last_finished_at": "2099-01-01T00:00:00+00:00"})
    assert public_status()["freshness"] == "degraded"
    assert load_receipt()["last_success_at"] == success
    save_receipt({"running": True, "last_exit_code": None})
    assert public_status()["freshness"] == "updating"
    restored = restore_run()
    assert restored["running"] is False
    assert restored["last_exit_code"] == 1
    assert restored["last_success_at"] == success
    assert public_status()["freshness"] == "degraded"


def test_cloud_requires_token_volume_and_single_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="ADMIN_API_TOKEN"):
        validate_cloud_config()
    monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 32)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "test")
    with pytest.raises(RuntimeError, match="persistent volume"):
        validate_cloud_config()
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    validate_cloud_config()
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path.parent / "outside"))
    with pytest.raises(RuntimeError, match="persistent volume"):
        validate_cloud_config()
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        validate_cloud_config()
