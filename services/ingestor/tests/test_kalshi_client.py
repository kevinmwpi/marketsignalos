from __future__ import annotations

import httpx
import pytest

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

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        client.list_trades(ticker="TEST-123")

    assert excinfo.value.response.status_code == 503


def test_keypair_auth_headers_are_attached() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(status_code=200, json={"trades": [], "cursor": None})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://example.invalid")
    client = KalshiClient(
        KalshiClientConfig(
            base_url="https://example.invalid",
            api_key_id="kid-123",
            private_key_pem="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        ),
        client=http_client,
    )
    client._sign_message = lambda _: "signed-message"  # type: ignore[method-assign]

    client.list_trades(ticker="TEST-123")

    assert captured_headers["kalshi-access-key"] == "kid-123"
    assert captured_headers["kalshi-access-signature"] == "signed-message"
    assert "kalshi-access-timestamp" in captured_headers


def test_keypair_auth_requires_both_values() -> None:
    client = KalshiClient(KalshiClientConfig(api_key_id="only-id"), client=httpx.Client())

    with pytest.raises(ValueError, match="must both be set"):
        client._build_keypair_headers(method="GET", path="/portfolio/trades")
