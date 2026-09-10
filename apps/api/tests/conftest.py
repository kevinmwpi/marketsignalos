from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def local_test_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Existing route tests exercise local operation without touching real data."""
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_ADMIN", "1")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("INGEST_EVERY_MINUTES", raising=False)
