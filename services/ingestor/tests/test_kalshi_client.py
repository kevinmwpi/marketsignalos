from __future__ import annotations

import httpx

from marketsignalos_ingestor.kalshi_client import KalshiClient, KalshiClientConfig


def test_list_trades_retries_on_rate_limit() -> None:
    requests_seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_seen
        requests_seen += 1
        if requests_seen == 1:
            return httpx.Response(status_code=429, headers={"Retry-After": "0"})
        return httpx.Response(status_code=200, json={"trades": [], "cursor": None})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://example.invalid")

    client = KalshiClient(
        KalshiClientConfig(base_url="https://example.invalid", max_retries=2),
        client=http_client,
    )

    payload = client.list_trades(ticker="TEST-123")
    assert payload["trades"] == []
    assert requests_seen == 2


def test_list_trades_raises_after_retry_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=503)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://example.invalid")

    client = KalshiClient(
        KalshiClientConfig(base_url="https://example.invalid", max_retries=1),
        client=http_client,
    )

    try:
        client.list_trades(ticker="TEST-123")
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 503
    else:
        raise AssertionError("Expected HTTPStatusError")
