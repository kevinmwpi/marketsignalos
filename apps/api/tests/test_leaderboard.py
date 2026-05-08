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


def test_leaderboard_returns_ranked_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fills_path = tmp_path / "fills.jsonl"
    resolutions_path = tmp_path / "resolutions.jsonl"
    enrichment_path = tmp_path / "enrichment.jsonl"

    fills: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []

    for index in range(20):
        market = f"KX-BEST-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-best",
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
        market = f"KX-MID-{index}"
        fills.append(
            {
                "source": "kalshi",
                "account_id": "acct-mid",
                "market_ticker": market,
                "fill_id": f"f-mid-{index}",
                "trade_id": f"t-mid-{index}",
                "side": "yes" if index < 12 else "no",
                "price": 55,
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
            "account_id": "acct-best",
            "cross_market_coordination": 0.9,
            "pre_resolution_accuracy": 0.9,
            "fill_intensity": 0.7,
        },
        {
            "account_id": "acct-mid",
            "cross_market_coordination": 0.1,
            "pre_resolution_accuracy": 0.2,
            "fill_intensity": 0.2,
        },
    ]

    _write_jsonl(fills_path, fills)
    _write_jsonl(resolutions_path, resolutions)
    _write_jsonl(enrichment_path, enrichment)

    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(fills_path))
    monkeypatch.setenv("INGESTOR_RESOLUTIONS_STORE_PATH", str(resolutions_path))
    monkeypatch.setenv("INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH", str(enrichment_path))

    client = TestClient(app)
    response = client.get("/signals/leaderboard?fresh_days=30&min_resolved=20&limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert [row["account_id"] for row in payload] == ["acct-best", "acct-mid"]
    assert payload[0]["skill_likelihood"] > payload[1]["skill_likelihood"]
    assert payload[0]["insider_like_score"] > payload[1]["insider_like_score"]

    required_fields = {
        "account_id", "account_first_seen_at", "account_age_days",
        "resolved_calls", "wins", "losses", "win_rate", "expected_wins",
        "excess_wins", "stddevs_above_expected", "skill_likelihood",
        "insider_like_score", "anomaly_probability", "last_activity_at",
    }
    for row in payload:
        assert required_fields <= row.keys(), f"leaderboard row missing fields: {row}"
        assert 0.0 <= row["win_rate"] <= 1.0
        assert 0.0 <= row["skill_likelihood"] <= 1.0
        assert 0.0 <= row["insider_like_score"] <= 1.0
        assert 0.0 <= row["anomaly_probability"] <= 1.0
        assert row["wins"] + row["losses"] == row["resolved_calls"]


def test_leaderboard_returns_empty_without_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INGESTOR_FILLS_STORE_PATH", str(tmp_path / "missing-fills.jsonl"))
    monkeypatch.setenv(
        "INGESTOR_RESOLUTIONS_STORE_PATH",
        str(tmp_path / "missing-resolutions.jsonl"),
    )
    monkeypatch.setenv(
        "INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH",
        str(tmp_path / "missing-enrichment.jsonl"),
    )

    client = TestClient(app)
    response = client.get("/signals/leaderboard")

    assert response.status_code == 200
    assert response.json() == []

