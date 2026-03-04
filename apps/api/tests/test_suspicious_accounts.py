from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketsignalos_api.main import app


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_suspicious_accounts_sorted_and_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fills_path = tmp_path / "fills.jsonl"
    resolutions_path = tmp_path / "resolutions.jsonl"

    fills: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []

    for index in range(20):
        market = f"KX-YES-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-young-best",
                "market_ticker": market,
                "fill_id": f"f-best-{index}",
                "trade_id": f"t-best-{index}",
                "side": "yes",
                "price": 60,
                "quantity": 1,
                "traded_at": "2026-03-01T00:00:00Z",
            }
        )
        resolutions.append(
            {
                "source": "kalshi",
                "market_ticker": market,
                "resolved_at": "2026-03-02T00:00:00Z",
                "settlement_side": "yes",
            }
        )

    for index in range(20):
        market = f"KX-MIX-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-young-mid",
                "market_ticker": market,
                "fill_id": f"f-mid-{index}",
                "trade_id": f"t-mid-{index}",
                "side": "yes" if index < 12 else "no",
                "price": 55,
                "quantity": 1,
                "traded_at": "2026-02-25T00:00:00Z",
            }
        )
        resolutions.append(
            {
                "source": "kalshi",
                "market_ticker": market,
                "resolved_at": "2026-03-02T00:00:00Z",
                "settlement_side": "yes",
            }
        )

    for index in range(20):
        market = f"KX-OLD-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-old",
                "market_ticker": market,
                "fill_id": f"f-old-{index}",
                "trade_id": f"t-old-{index}",
                "side": "yes",
                "price": 52,
                "quantity": 1,
                "traded_at": "2025-01-01T00:00:00Z",
            }
        )
        resolutions.append(
            {
                "source": "kalshi",
                "market_ticker": market,
                "resolved_at": "2025-01-02T00:00:00Z",
                "settlement_side": "yes",
            }
        )

    _write_jsonl(fills_path, fills)
    _write_jsonl(resolutions_path, resolutions)

    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(fills_path))
    monkeypatch.setenv("INGESTOR_RESOLUTIONS_STORE_PATH", str(resolutions_path))

    client = TestClient(app)
    response = client.get("/signals/suspicious-accounts?fresh_days=30&min_resolved=20&limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert [row["account_id"] for row in payload] == ["acct-young-best", "acct-young-mid"]
    assert payload[0]["resolved_calls"] == 20
    assert payload[0]["wins"] == 20
    assert payload[0]["losses"] == 0
    assert payload[1]["wins"] == 12
    assert payload[1]["losses"] == 8


def test_suspicious_accounts_returns_empty_without_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(tmp_path / "missing-fills.jsonl"))
    monkeypatch.setenv("INGESTOR_RESOLUTIONS_STORE_PATH", str(tmp_path / "missing-resolutions.jsonl"))

    client = TestClient(app)
    response = client.get("/signals/suspicious-accounts")

    assert response.status_code == 200
    assert response.json() == []
