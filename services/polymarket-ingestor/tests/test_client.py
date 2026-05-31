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


# ── Recent on-chain traders (subgraph) ──────────────────────────────────────


def _ofe(maker: str, taker: str, ts: int) -> dict[str, Any]:
    """One orderFilledEvent as the subgraph returns it (timestamp is a BigInt
    string, maker/taker are Account objects)."""
    return {"maker": {"id": maker}, "taker": {"id": taker}, "timestamp": str(ts)}


def test_get_recent_trader_wallets_collects_distinct_lowercased() -> None:
    pages = [
        {"data": {"orderFilledEvents": [
            _ofe("0xAAA", "0xBBB", 100),
            _ofe("0xCCC", "0xAAA", 99),  # 0xAAA repeats older — deduped, keeps ts=100
        ]}},
        {"data": {"orderFilledEvents": [_ofe("0xDDD", "0xEEE", 98)]}},  # short page → stop
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "api.goldsky.com"
        body = pages[min(idx["n"], len(pages) - 1)]
        idx["n"] += 1
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)
    wallets = client.get_recent_trader_wallets(max_wallets=10, page_size=2, max_pages=5)
    client.close()
    # Distinct + lowercased, ordered newest-first by most-recent ts, addr tiebreak.
    assert wallets == ["0xaaa", "0xbbb", "0xccc", "0xddd", "0xeee"]


def test_get_recent_trader_wallets_paginates_with_timestamp_cursor() -> None:
    queries: list[str] = []
    pages = [
        {"data": {"orderFilledEvents": [_ofe("0xa", "0xb", 100), _ofe("0xc", "0xd", 95)]}},
        {"data": {"orderFilledEvents": [_ofe("0xe", "0xf", 90)]}},
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        queries.append(json.loads(request.content.decode())["query"])
        body = pages[min(idx["n"], len(pages) - 1)]
        idx["n"] += 1
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)
    client.get_recent_trader_wallets(max_wallets=10, page_size=2, max_pages=5)
    client.close()
    # First page has no cursor; the second walks strictly below the oldest ts seen.
    assert "timestamp_lt" not in queries[0]
    assert "timestamp_lt: 95" in queries[1]


def test_get_recent_trader_wallets_respects_max_wallets() -> None:
    """A handler that always returns a full page must still terminate once
    max_wallets distinct addresses are collected."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orderFilledEvents": [
            _ofe("0x01", "0x02", 100), _ofe("0x03", "0x04", 99),
        ]}})

    client = _client_with_mock(handler)
    wallets = client.get_recent_trader_wallets(max_wallets=3, page_size=2, max_pages=100)
    client.close()
    assert len(wallets) == 3


def test_get_recent_trader_wallets_empty_first_page() -> None:
    client = _client_with_mock(
        lambda r: httpx.Response(200, json={"data": {"orderFilledEvents": []}})
    )
    assert client.get_recent_trader_wallets(max_wallets=5) == []
    client.close()


def test_get_recent_trader_wallets_raises_on_graphql_errors() -> None:
    client = _client_with_mock(
        lambda r: httpx.Response(200, json={"errors": [{"message": "bad query"}]})
    )
    with pytest.raises(ValueError, match="subgraph returned errors"):
        client.get_recent_trader_wallets(max_wallets=5)
    client.close()


def test_get_recent_trader_wallets_retries_transient_statement_timeout() -> None:
    """A server-side statement-timeout arrives as an HTTP 200 GraphQL error, so
    it never tripped the status-based retry. It should be retried like a 5xx and
    succeed on the next attempt rather than failing the call."""
    timeout_body = {"errors": [{"message": (
        "Failed to get entities from store: canceling statement due to "
        "statement timeout, query = ..."
    )}]}
    success_body = {"data": {"orderFilledEvents": [_ofe("0xAAA", "0xBBB", 100)]}}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=timeout_body if calls["n"] == 1 else success_body)

    client = _client_with_mock(handler)  # config: max_retries=2
    wallets = client.get_recent_trader_wallets(max_wallets=5, page_size=2, max_pages=1)
    client.close()
    assert calls["n"] == 2  # first timed out, retry succeeded
    assert wallets == ["0xaaa", "0xbbb"]


def test_get_recent_trader_wallets_raises_when_timeout_persists() -> None:
    """If the statement-timeout never clears, the retry budget is exhausted and
    the error still surfaces — so the deep pipeline's non-fatal guard can catch
    it and proceed without subgraph wallets."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"errors": [{"message":
            "canceling statement due to statement timeout"}]})

    client = _client_with_mock(handler)  # config: max_retries=2
    with pytest.raises(ValueError, match="subgraph returned errors"):
        client.get_recent_trader_wallets(max_wallets=5)
    client.close()
    assert calls["n"] == 3  # initial attempt + 2 retries


def test_get_recent_trader_wallets_validates_args() -> None:
    client = _client_with_mock(
        lambda r: httpx.Response(200, json={"data": {"orderFilledEvents": []}})
    )
    assert client.get_recent_trader_wallets(max_wallets=0) == []  # no-op, no HTTP
    with pytest.raises(ValueError, match="page_size"):
        client.get_recent_trader_wallets(page_size=0)
    with pytest.raises(ValueError, match="page_size"):
        client.get_recent_trader_wallets(page_size=1001)
    client.close()
