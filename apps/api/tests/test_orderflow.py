from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def test_orderflow_detects_odds_jumps_and_large_bets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trade_store = tmp_path / "trades.jsonl"
    trade_store.write_text(
        "\n".join(
            [
                '{"source":"kalshi","market_ticker":"MKT-1","trade_id":"1","side":"yes","price":50,"quantity":10,"traded_at":"2026-01-01T00:00:00Z"}',
                '{"source":"kalshi","market_ticker":"MKT-1","trade_id":"2","side":"yes","price":67,"quantity":12,"traded_at":"2026-01-01T00:01:00Z"}',
                '{"source":"kalshi","market_ticker":"MKT-1","trade_id":"3","side":"no","price":66,"quantity":250,"traded_at":"2026-01-01T00:02:00Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(trade_store))

    client = TestClient(app)
    response = client.get("/signals/orderflow?min_odds_jump=10&min_large_quantity=100")

    assert response.status_code == 200
    payload = response.json()
    anomaly_types = {row["anomaly_type"] for row in payload}
    assert "odds_jump" in anomaly_types
    assert "large_bet" in anomaly_types


def test_orderflow_returns_empty_when_store_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(tmp_path / "missing.jsonl"))

    client = TestClient(app)
    response = client.get("/signals/orderflow")

    assert response.status_code == 200
    assert response.json() == []
