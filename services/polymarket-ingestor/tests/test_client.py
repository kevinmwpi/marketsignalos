from __future__ import annotations

from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.polymarket_client import (
    DATA_API,
    GAMMA_API,
    LB_API,
    PolymarketClient,
    PolymarketClientConfig,
)


def _client_with_mock(handler: Any) -> PolymarketClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"Accept": "application/json"})
    return PolymarketClient(
        config=PolymarketClientConfig(max_retries=2, retry_backoff_seconds=0.001),
        client=http,
    )


def test_get_leaderboard_returns_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "lb-api.polymarket.com"
        assert request.url.path == "/profit"
        assert dict(request.url.params) == {"window": "all", "limit": "10"}
        return httpx.Response(200, json=[{"proxyWallet": "0xabc", "amount": 1.0, "pseudonym": "x", "name": "x"}])

    client = _client_with_mock(handler)
    rows = client.get_leaderboard(metric="profit", window="all", limit=10)
    assert rows[0]["proxyWallet"] == "0xabc"
    client.close()


def test_get_leaderboard_rejects_bad_metric() -> None:
    client = _client_with_mock(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="metric must be"):
        client.get_leaderboard(metric="roi")
    client.close()


def test_get_trader_leaderboard_rankings_passes_matrix_params() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"proxyWallet": "0xabc", "amount": 1.0,
                                          "pseudonym": "x", "name": "x"}])

    client = _client_with_mock(handler)
    rows = client.get_trader_leaderboard_rankings(
        category="POLITICS", time_period="WEEK", order_by="VOL", limit=25, offset=50,
    )
    assert captured["host"] == "data-api.polymarket.com"
    assert captured["path"] == "/v1/leaderboard"
    assert captured["params"] == {
        "category": "POLITICS",
        "timePeriod": "WEEK",
        "orderBy": "VOL",
        "limit": "25",
        "offset": "50",
    }
    assert rows[0]["proxyWallet"] == "0xabc"
    client.close()


def test_get_trader_leaderboard_rankings_unwraps_data_envelope() -> None:
    """Some Polymarket data-api endpoints wrap arrays in {data: [...]} — accept either."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"proxyWallet": "0xdef", "amount": 2.0, "pseudonym": "y", "name": "y"},
        ]})

    client = _client_with_mock(handler)
    rows = client.get_trader_leaderboard_rankings(
        category="OVERALL", time_period="ALL", order_by="PNL", limit=10,
    )
    assert rows[0]["proxyWallet"] == "0xdef"
    client.close()


def test_get_trader_leaderboard_rankings_omits_zero_offset() -> None:
    """offset=0 should be elided from the query string (matches the activity client)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_trader_leaderboard_rankings(
        category="OVERALL", time_period="ALL", order_by="PNL", limit=10, offset=0,
    )
    assert "offset" not in captured["params"]
    client.close()


def test_get_trader_leaderboard_rankings_validates_args() -> None:
    client = _client_with_mock(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="category"):
        client.get_trader_leaderboard_rankings(category="BOGUS")
    with pytest.raises(ValueError, match="time_period"):
        client.get_trader_leaderboard_rankings(time_period="QUARTER")
    with pytest.raises(ValueError, match="order_by"):
        client.get_trader_leaderboard_rankings(order_by="ROI")
    with pytest.raises(ValueError, match="limit"):
        client.get_trader_leaderboard_rankings(limit=0)
    with pytest.raises(ValueError, match="limit"):
        client.get_trader_leaderboard_rankings(limit=51)
    with pytest.raises(ValueError, match="offset"):
        client.get_trader_leaderboard_rankings(offset=-1)
    with pytest.raises(ValueError, match="offset"):
        client.get_trader_leaderboard_rankings(offset=1001)
    client.close()


def test_get_wallet_activity_passes_user_param() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_wallet_activity("0xABC", limit=5)
    assert captured["host"] == "data-api.polymarket.com"
    assert captured["path"] == "/activity"
    assert captured["params"]["user"] == "0xABC"
    assert captured["params"]["limit"] == "5"
    client.close()


def test_get_wallet_value_unwraps_single_element_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"user": "0xabc", "value": 1234.5}])

    client = _client_with_mock(handler)
    result = client.get_wallet_value("0xabc")
    assert result["value"] == 1234.5
    client.close()


def test_get_wallet_value_handles_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    result = client.get_wallet_value("0xabc")
    assert result == {"user": "0xabc", "value": 0}
    client.close()


def test_get_markets_serializes_bool_params() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_markets(active=True, closed=False, limit=2)
    assert captured["params"]["active"] == "true"
    assert captured["params"]["closed"] == "false"
    client.close()


def test_retry_on_503_then_succeeds() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"proxyWallet": "0xabc", "amount": 1, "pseudonym": "", "name": ""}])

    client = _client_with_mock(handler)
    rows = client.get_leaderboard()
    assert call_count["n"] == 2
    assert len(rows) == 1
    client.close()


def test_retry_exhausted_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client_with_mock(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_leaderboard()
    client.close()


def test_host_constants_unchanged() -> None:
    # Tripwire: if these change, every existing JSONL filename assumption breaks too.
    assert LB_API.endswith("lb-api.polymarket.com")
    assert DATA_API.endswith("data-api.polymarket.com")
    assert GAMMA_API.endswith("gamma-api.polymarket.com")
