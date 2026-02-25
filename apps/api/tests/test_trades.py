from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def test_trades_endpoint_returns_latest_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trade_store = tmp_path / "trades.jsonl"
    trade_store.write_text(
        "\n".join(
            [
                '{"source":"kalshi","market_ticker":"T1","trade_id":"1","side":"yes","price":61,"quantity":2,"traded_at":"2026-01-01T00:00:00Z"}',
                '{"source":"kalshi","market_ticker":"T2","trade_id":"2","side":"no","price":44,"quantity":1,"traded_at":"2026-01-01T00:01:00Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(trade_store))

    client = TestClient(app)
    response = client.get("/signals/trades?limit=1")

    assert response.status_code == 200
    assert response.json() == [
        {
            "source": "kalshi",
            "market_ticker": "T2",
            "trade_id": "2",
            "side": "no",
            "price": 44.0,
            "quantity": 1,
            "traded_at": "2026-01-01T00:01:00Z",
        }
    ]


def test_trades_endpoint_returns_empty_when_store_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(tmp_path / "missing.jsonl"))

    client = TestClient(app)
    response = client.get("/signals/trades")

    assert response.status_code == 200
    assert response.json() == []
