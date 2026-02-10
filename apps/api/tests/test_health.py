from __future__ import annotations

from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def test_health_returns_ok() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_endpoint_exists() -> None:
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "version=" in resp.headers["content-type"]
    # should look like Prometheus text exposition format
    assert "# HELP" in resp.text
    assert "# TYPE" in resp.text
