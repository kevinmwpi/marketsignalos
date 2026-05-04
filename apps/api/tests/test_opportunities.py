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


def test_opportunities_combines_skill_and_orderflow_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fills_path = tmp_path / "fills.jsonl"
    resolutions_path = tmp_path / "resolutions.jsonl"
    enrichment_path = tmp_path / "enrichment.jsonl"
    trades_path = tmp_path / "trades.jsonl"

    fills: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    for index in range(25):
        market = f"KX-SKILL-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-skilled",
                "market_ticker": market,
                "fill_id": f"fill-{index}",
                "trade_id": f"trade-{index}",
                "side": "yes",
                "price": 58,
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

    enrichment = [
        {
            "account_id": "acct-skilled",
            "cross_market_coordination": 0.8,
            "pre_resolution_accuracy": 0.9,
            "fill_intensity": 0.7,
        }
    ]
    trades = [
        {
            "source": "kalshi",
            "market_ticker": "KX-FLOW-1",
            "trade_id": "flow-1",
            "side": "yes",
            "price": 50,
            "quantity": 10,
            "traded_at": "2026-03-01T00:00:00Z",
        },
        {
            "source": "kalshi",
            "market_ticker": "KX-FLOW-1",
            "trade_id": "flow-2",
            "side": "yes",
            "price": 51,
            "quantity": 12,
            "traded_at": "2026-03-01T00:01:00Z",
        },
        {
            "source": "kalshi",
            "market_ticker": "KX-FLOW-1",
            "trade_id": "flow-3",
            "side": "yes",
            "price": 52,
            "quantity": 11,
            "traded_at": "2026-03-01T00:02:00Z",
        },
        {
            "source": "kalshi",
            "market_ticker": "KX-FLOW-1",
            "trade_id": "flow-4",
            "side": "yes",
            "price": 70,
            "quantity": 250,
            "traded_at": "2026-03-01T00:03:00Z",
        },
    ]

    _write_jsonl(fills_path, fills)
    _write_jsonl(resolutions_path, resolutions)
    _write_jsonl(enrichment_path, enrichment)
    _write_jsonl(trades_path, trades)

    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(fills_path))
    monkeypatch.setenv("INGESTOR_RESOLUTIONS_STORE_PATH", str(resolutions_path))
    monkeypatch.setenv("INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH", str(enrichment_path))
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(trades_path))

    client = TestClient(app)
    response = client.get(
        "/signals/opportunities"
        "?fresh_days=30&min_resolved=20&limit=10"
        "&min_odds_jump=10&min_size_zscore=2&min_large_quantity=100"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["candidate_count"] == 2
    assert payload["summary"]["skilled_account_count"] == 1
    assert payload["summary"]["orderflow_event_count"] == 1

    signal_types = {row["signal_type"] for row in payload["opportunities"]}
    assert signal_types == {"skilled_account", "orderflow_dislocation"}

    account = next(
        row for row in payload["opportunities"] if row["signal_type"] == "skilled_account"
    )
    assert account["entity"] == "acct-skilled"
    assert account["probability"] > 0.99

    flow = next(
        row for row in payload["opportunities"] if row["signal_type"] == "orderflow_dislocation"
    )
    assert flow["market_ticker"] == "KX-FLOW-1"
    assert flow["direction"] == "yes"
    assert {driver["label"] for driver in flow["drivers"]} == {
        "large quantity",
        "price displacement",
        "volume z-score",
    }


def test_opportunities_returns_empty_dashboard_without_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(tmp_path / "missing-fills.jsonl"))
    monkeypatch.setenv("INGESTOR_RESOLUTIONS_STORE_PATH", str(tmp_path / "missing-resolutions.jsonl"))
    monkeypatch.setenv("INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH", str(tmp_path / "missing-enrichment.jsonl"))
    monkeypatch.setenv("INGESTOR_TRADE_STORE_PATH", str(tmp_path / "missing-trades.jsonl"))

    client = TestClient(app)
    response = client.get("/signals/opportunities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "candidate_count": 0,
        "skilled_account_count": 0,
        "orderflow_event_count": 0,
        "top_probability": 0.0,
        "top_opportunity_score": 0.0,
        "freshness_window_days": 30,
    }
    assert payload["opportunities"] == []
