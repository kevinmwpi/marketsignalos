from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ARTIFACT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("api_adapter", ARTIFACT / "api_adapter.py")
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INGEST_EVERY_MINUTES", "0")
    monkeypatch.setenv("FASTLANE_EVERY_SECONDS", "0")
    monkeypatch.setenv("INGEST_POLYMARKET", "0")
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)


def test_mounted_lifespan_runs_and_real_product_routes_return_json(monkeypatch: pytest.MonkeyPatch):
    events = []

    @asynccontextmanager
    async def imported_lifespan(_):
        events.append("startup")
        yield
        events.append("shutdown")

    monkeypatch.setattr(adapter.imported_app.router, "lifespan_context", imported_lifespan)
    with TestClient(adapter.app) as client:
        assert events == ["startup"]
        for path in ("/api/healthz", "/api/health", "/api/signals/skilled-bets?limit=1",
                     "/api/signals/skilled-bets/summary", "/api/signals/exits?limit=1",
                     "/api/signals/polymarket-leaderboard?limit=1"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"].startswith("application/json")
        assert client.get("/signals/skilled-bets").status_code == 404
        missing_wallet = client.get("/api/signals/wallets/0xmissing")
        assert missing_wallet.status_code == 404
        assert missing_wallet.json()["detail"] == "No enrichment data for wallet 0xmissing"
    assert events == ["startup", "shutdown"]


@pytest.mark.parametrize("path,method", [
    ("/api/ingestor/status", "GET"), ("/api/ingestor/watchlist", "GET"),
    ("/api/metrics", "GET"), ("/api/signals/notifications/status", "GET"),
    ("/api/signals/fastlane/status", "GET"), ("/api/ingestor/run", "POST"),
    ("/api/ingestor/run/deep", "POST"), ("/api/ingestor/watchlist", "POST"),
    ("/api/ingestor/prune-wallets", "POST"), ("/api/signals/exits/refresh", "POST"),
    ("/api/signals/notifications/run", "POST"), ("/api/signals/fastlane/run", "POST"),
])
def test_private_routes_fail_closed(path, method, monkeypatch):
    client = TestClient(adapter.app)
    assert client.request(method, path).status_code == 503
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-token-" * 4)
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_operator_can_read_status_without_exposing_it_to_public(monkeypatch):
    token = "test-token-" * 4
    monkeypatch.setenv("ADMIN_API_TOKEN", token)
    response = TestClient(adapter.app).get("/api/ingestor/status",
                                         headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "running" in response.json()


def test_production_command_starts_python_api_outside_repo_directory(tmp_path):
    config = tomllib.loads((ARTIFACT / ".replit-artifact/artifact.toml").read_text())
    service = config["services"][0]
    production = service["production"]
    assert production["build"]["args"] == ["uv", "sync", "--frozen", "--no-dev"]
    command = production["run"]["args"]
    assert command == [".venv/bin/python", "artifacts/api-server/start.py"]
    assert service["development"]["run"] == "python artifacts/api-server/start.py"
    assert production["health"]["startup"]["path"] == "/api/healthz"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = {**os.environ, **production["run"]["env"], "PORT": str(port),
           "POLYMARKET_DATA_DIR": str(tmp_path / "data"), "ADMIN_API_TOKEN": ""}
    # Use the test interpreter for installed dependencies, executing the exact
    # declared launcher. No Replit-specific absolute paths or PYTHONPATH required.
    with (tmp_path / "server.log").open("w+") as log, subprocess.Popen(
        [sys.executable, str(ARTIFACT / "start.py")], cwd=tmp_path, env=env,
        stdout=log, stderr=log,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False) as client:
                while True:
                    try:
                        response = client.get("/api/healthz")
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            pytest.fail(log.read())
                        time.sleep(0.05)
                assert response.json() == {"status": "ok"}
                assert client.get("/api/signals/skilled-bets?limit=1").json() == []
                assert client.get("/api/signals/polymarket-leaderboard?limit=1").json() == []
                assert client.get("/api/ingestor/status").status_code == 503
                assert (tmp_path / "data" / "skilled_bets_feed_cache.json").is_file()
        finally:
            process.terminate()
            process.wait(timeout=10)
