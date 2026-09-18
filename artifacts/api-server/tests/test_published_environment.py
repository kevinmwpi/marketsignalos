"""Exercise the actual artifact build/run commands in a clean publication tree."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_production_uses_built_environment_without_dot_venv(tmp_path: Path) -> None:
    assert shutil.which("uv"), "Install uv to test the declared production commands"
    published = tmp_path / "published"
    published.mkdir()
    for relative in (
        "pyproject.toml", "uv.lock", "artifacts/api-server/api_adapter.py",
        "artifacts/api-server/start.py", "artifacts/api-server/.replit-artifact/artifact.toml",
    ):
        target = published / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for relative in (
        ".migration-backup/apps/api/src", ".migration-backup/services/polymarket-ingestor/src",
    ):
        shutil.copytree(ROOT / relative, published / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    config = tomllib.loads((published / "artifacts/api-server/.replit-artifact/artifact.toml").read_text())
    production = config["services"][0]["production"]
    # Replit's python-base module overrides uv's default environment location.
    # Build a fresh environment rather than reusing the test interpreter's packages.
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(published / ".pythonlibs"),
           "UV_PYTHON": sys.executable}
    env.pop("VIRTUAL_ENV", None)
    build = subprocess.run(production["build"]["args"], cwd=published,
                           env={**env, **production["build"].get("env", {})},
                           capture_output=True, text=True, timeout=90, check=False)
    assert build.returncode == 0, build.stderr
    assert (published / ".pythonlibs").is_dir()
    assert not (published / ".venv").exists()

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env.update(production["run"].get("env", {}))
    env.update({"PORT": str(port), "POLYMARKET_DATA_DIR": str(tmp_path / "data"),
                "ADMIN_API_TOKEN": "", "UV_OFFLINE": "1",
                "UV_CACHE_DIR": str(tmp_path / "empty-runtime-cache")})
    # Do not rewrite the executable to sys.executable: that hid the missing
    # .venv/bin/python error in the earlier launcher-only smoke test.
    with (tmp_path / "published.log").open("w+") as log:
        process = subprocess.Popen(production["run"]["args"], cwd=published, env=env,
                                   stdout=log, stderr=log, start_new_session=os.name != "nt")
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1,
                              trust_env=False, follow_redirects=False) as client:
                while True:
                    try:
                        health = client.get(production["health"]["startup"]["path"])
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            pytest.fail(log.read())
                        time.sleep(0.05)
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}
                for path in ("/api/signals/skilled-bets?limit=1", "/api/signals/exits?limit=1",
                             "/api/signals/polymarket-leaderboard?limit=1"):
                    response = client.get(path)
                    assert response.status_code == 200
                    assert response.json() == []
                summary = client.get("/api/signals/skilled-bets/summary")
                assert summary.status_code == 200
                assert isinstance(summary.json(), dict)
                assert client.post("/api/ingestor/run").status_code == 503
                assert not (published / ".venv").exists()
        finally:
            # Stop uv and its child API, including when an assertion fails.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)
