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


def _page(rows: list[dict[str, Any]], next_cursor: str | None = None) -> dict[str, Any]:
    """A Data API v2 listing response."""
    return {
        "data": rows,
        "pagination": {
            "limit": len(rows), "offset": 0,
            "has_more": next_cursor is not None, "next_cursor": next_cursor,
        },
    }


def _capture(body: Any) -> tuple[PolymarketClient, dict[str, Any]]:
    """A client whose every request is answered with ``body`` and recorded."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=body)

    return _client_with_mock(handler), captured


def test_leaderboard_page_sends_v2_names_and_values() -> None:
    client, captured = _capture(_page([{"user_id": "0xabc", "pnl": 1.0}], "next"))
    page = client.get_trader_leaderboard_page(
        category="POLITICS", time_period="WEEK", order_by="VOL", limit=25, cursor="c1",
    )
    client.close()
    assert captured["host"] == "data-api.polymarket.com"
    assert captured["path"] == "/v2/leaderboard"
    # v2 wants lower-case periods and VOLUME: it answers 400 to v1's "VOL".
    assert captured["params"] == {
        "category": "POLITICS", "time_period": "week", "sort_by": "VOLUME",
        "limit": "25", "cursor": "c1",
    }
    assert page.rows[0]["user_id"] == "0xabc"
    assert page.next_cursor == "next"


def test_leaderboard_first_page_sends_no_cursor_and_the_last_page_returns_none() -> None:
    client, captured = _capture(_page([]))
    page = client.get_trader_leaderboard_page(order_by="PNL", limit=10)
    client.close()
    assert "cursor" not in captured["params"]
    assert captured["params"]["sort_by"] == "PNL"
    assert page.rows == [] and page.next_cursor is None


def test_leaderboard_page_validates_args() -> None:
    client = _client_with_mock(lambda r: httpx.Response(200, json=_page([])))
    with pytest.raises(ValueError, match="category"):
        client.get_trader_leaderboard_page(category="BOGUS")
    with pytest.raises(ValueError, match="time_period"):
        client.get_trader_leaderboard_page(time_period="QUARTER")
    with pytest.raises(ValueError, match="order_by"):
        client.get_trader_leaderboard_page(order_by="ROI")
    with pytest.raises(ValueError, match="limit"):
        client.get_trader_leaderboard_page(limit=0)
    with pytest.raises(ValueError, match="limit"):
        client.get_trader_leaderboard_page(limit=1001)
    client.close()


def test_wallet_standing_is_one_object() -> None:
    client, captured = _capture({"data": {"user_id": "0xabc", "pnl": 12.5, "volume": 40.0}})
    standing = client.get_wallet_leaderboard_standing("0xabc", time_period="MONTH")
    client.close()
    assert captured["path"] == "/v2/leaderboard"
    assert captured["params"] == {
        "user": "0xabc", "category": "OVERALL", "time_period": "month",
    }
    assert standing["pnl"] == 12.5


def test_a_wallet_with_no_standing_in_the_period_is_empty_not_zero() -> None:
    client, _ = _capture({"data": None})
    assert client.get_wallet_leaderboard_standing("0xabc", time_period="MONTH") == {}
    client.close()


def test_wallet_volume_asks_for_the_whole_history() -> None:
    client, captured = _capture({"data": {"volume": 9.0, "volume_usdc": 4.5, "trade_count": 3}})
    volume = client.get_wallet_volume("0xabc")
    client.close()
    assert captured["path"] == "/v2/user-volume"
    assert captured["params"] == {"user": "0xabc", "start": "1"}
    assert volume["volume_usdc"] == 4.5


def test_activity_page_asks_for_all_history_newest_first_without_deposits() -> None:
    client, captured = _capture(_page([]))
    client.get_wallet_activity_page("0xABC", limit=5)
    client.close()
    assert captured["host"] == "data-api.polymarket.com"
    assert captured["path"] == "/v2/activity"
    # Without ``start`` v2 serves three years only; start=1 is the whole history.
    assert captured["params"] == {
        "user": "0xABC", "limit": "5", "sort_direction": "DESC", "start": "1",
        "exclude_deposits_withdrawals": "true",
    }


def test_activity_page_passes_the_window_and_the_cursor() -> None:
    client, captured = _capture(_page([{"timestamp": 150}], "c2"))
    page = client.get_wallet_activity_page("0xabc", limit=500, cursor="c1", start=100, end=200)
    client.close()
    assert captured["params"] == {
        "user": "0xabc", "limit": "500", "sort_direction": "DESC", "start": "100",
        "exclude_deposits_withdrawals": "true", "end": "200", "cursor": "c1",
    }
    assert "offset" not in captured["params"]  # v2 answers 400 to it
    assert page.rows == [{"timestamp": 150}] and page.next_cursor == "c2"


def test_positions_page_lists_current_holdings_by_default() -> None:
    client, captured = _capture(_page([]))
    client.get_wallet_positions_page("0xabc", limit=500)
    client.close()
    assert captured["path"] == "/v2/positions"
    assert captured["params"] == {"user": "0xabc", "limit": "500"}


def test_positions_page_passes_status_floor_ordering_and_cursor() -> None:
    client, captured = _capture(_page([]))
    client.get_wallet_positions_page(
        "0xabc", status="CLOSED", limit=50, cursor="c1", min_shares=1, sort_by="TOKENS",
    )
    client.close()
    assert captured["params"] == {
        "user": "0xabc", "limit": "50", "status": "CLOSED", "filter_type": "TOKENS",
        "filter_amount": "1", "sort_by": "TOKENS", "sort_direction": "DESC", "cursor": "c1",
    }


def test_positions_page_takes_one_floor_shares_or_value() -> None:
    client, captured = _capture(_page([]))
    client.get_wallet_positions_page("0xabc", min_value_usdc=0.000001, sort_by="TOKENS")
    assert captured["params"] == {
        "user": "0xabc", "limit": "500", "filter_type": "CASH", "filter_amount": "1e-06",
        "sort_by": "TOKENS", "sort_direction": "DESC",
    }
    with pytest.raises(ValueError, match="one position floor"):
        client.get_wallet_positions_page("0xabc", min_shares=1, min_value_usdc=0.01)
    client.close()


@pytest.mark.parametrize("body", [
    [],  # v1's bare list
    {"data": []},  # no pagination object
    {"data": {"user_id": "0xabc"}, "pagination": {}},
    {"pagination": {"has_more": False, "next_cursor": None}},
])
def test_a_listing_without_the_v2_envelope_is_refused(body: Any) -> None:
    client, _ = _capture(body)
    with pytest.raises(ValueError, match="Expected"):
        client.get_wallet_activity_page("0xabc")
    client.close()


def test_a_page_that_reports_more_rows_but_gives_no_cursor_is_refused() -> None:
    """Followed blindly it would end the walk and silently truncate a history."""
    client, _ = _capture(
        {"data": [{"timestamp": 1}], "pagination": {"has_more": True, "next_cursor": None}}
    )
    with pytest.raises(ValueError, match="has_more"):
        client.get_wallet_activity_page("0xabc")
    client.close()


def test_a_retired_v1_route_fails_without_retrying() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(410, json={"error": "Data API v1 has been retired"})

    client = _client_with_mock(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_wallet_value("0xabc")
    client.close()
    assert calls["n"] == 1


def test_get_markets_by_condition_ids_passes_explicit_closed_filter() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_markets_by_condition_ids(["0xa", "0xb"], closed=True)
    client.close()
    assert captured[0]["closed"] == "true"


def test_wallet_value_reads_the_data_object() -> None:
    client, captured = _capture({"data": {"proxy_wallet": "0xabc", "value": 1234.5}})
    result = client.get_wallet_value("0xabc")
    client.close()
    assert captured["path"] == "/v2/value"
    assert captured["params"] == {"user": "0xabc"}
    assert result["value"] == 1234.5


def test_wallet_value_without_data_is_zero() -> None:
    client, _ = _capture({"data": None})
    assert client.get_wallet_value("0xABC") == {"proxy_wallet": "0xabc", "value": 0}
    client.close()


def test_wallet_value_refuses_v1s_one_element_list() -> None:
    client, _ = _capture([{"user": "0xabc", "value": 1234.5}])
    with pytest.raises(ValueError, match="Expected"):
        client.get_wallet_value("0xabc")
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


def test_retry_on_read_timeout_then_succeeds() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json=[{"proxyWallet": "0xabc", "amount": 1, "pseudonym": "", "name": ""}])

    client = _client_with_mock(handler)
    rows = client.get_leaderboard()
    assert call_count["n"] == 2
    assert len(rows) == 1
    client.close()


def test_retry_transport_error_exhausted_raises() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _client_with_mock(handler)
    with pytest.raises(httpx.ReadTimeout):
        client.get_leaderboard()
    # max_retries=2 → one initial attempt + two retries
    assert call_count["n"] == 3
    client.close()


def test_cookie_jar_never_accumulates() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            headers={"Set-Cookie": f"__cf_bm_{call_count['n']}=v{call_count['n']}; Path=/"},
            json=[{"proxyWallet": "0xabc", "amount": 1, "pseudonym": "", "name": ""}],
        )

    client = _client_with_mock(handler)
    for _ in range(3):
        client.get_leaderboard()
    # Request building walks the whole jar, so it must stay empty — a jar
    # that grows per response degrades large backfills to a crawl.
    assert len(client._client.cookies) == 0
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
