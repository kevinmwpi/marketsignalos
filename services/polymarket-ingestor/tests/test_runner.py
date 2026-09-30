from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket.kalshi_markets_fetch import (
    KalshiMarket,
    write_kalshi_markets_jsonl,
)
from marketsignalos_polymarket.models import PolymarketActivity, PolymarketMarket
from marketsignalos_polymarket.polymarket_client import (
    PolymarketClient,
    PolymarketClientConfig,
)
from marketsignalos_polymarket.runner import (
    _activity_key,
    _build_stores,
    _EconomicsPeriodEntry,
    _is_kalshi_parlay,
    _paginate_activity,
    _paginate_activity_window,
    _paginate_positions,
    _plan_market_backfill_fetches,
    _select_shallow_wallet_targets,
    _write_economics_cache,
    parse_activity_row,
    parse_leaderboard_row,
    parse_market_row,
    parse_position_row,
    run_markets_backfill_from_activity,
    run_match_markets,
    run_pipeline,
    run_wallets,
    seed_watchlist_from_leaderboard,
)


def _client_with_handler(handler: Any) -> PolymarketClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"Accept": "application/json"})
    return PolymarketClient(
        config=PolymarketClientConfig(max_retries=1, retry_backoff_seconds=0.001),
        client=http,
    )


def _activity_row(ts: int, tx: str, cond: str = "0xc", oi: int = 0) -> dict[str, Any]:
    return {
        "proxyWallet": "0xabc",
        "timestamp": ts,
        "conditionId": cond,
        "type": "TRADE",
        "size": 1.0,
        "usdcSize": 0.5,
        "transactionHash": tx,
        "price": 0.5,
        "side": "BUY",
        "outcomeIndex": oi,
        "title": "t",
        "slug": "s",
        "eventSlug": "e",
        "outcome": "Yes",
    }


def test_parse_leaderboard_row_matches_real_shape() -> None:
    raw = {
        "proxyWallet": "0x56687BF447DB6FFA42FFE2204A05EDAA20F55839",
        "amount": 22053933.75,
        "pseudonym": "Theo4",
        "name": "Theo4",
        "bio": "",
        "profileImage": "",
        "profileImageOptimized": "",
    }
    entry = parse_leaderboard_row(raw, metric="profit", window="all")
    # Wallets are lowercased so the dedupe key is stable.
    assert entry.proxy_wallet == "0x56687bf447db6ffa42ffe2204a05edaa20f55839"
    assert entry.amount_usdc == 22053933.75
    assert entry.metric == "profit"


def test_parse_activity_row_matches_real_shape() -> None:
    raw = {
        "proxyWallet": "0xabc",
        "timestamp": 1715000000,
        "conditionId": "0xcond",
        "type": "TRADE",
        "size": 100.0,
        "usdcSize": 62.0,
        "transactionHash": "0xtx1",
        "price": 0.62,
        "asset": "12345",
        "side": "BUY",
        "outcomeIndex": 0,
        "title": "Some market",
        "slug": "some-market",
        "icon": "https://...",
        "eventSlug": "some-event",
        "outcome": "Yes",
        "name": "Theo4",
        "pseudonym": "Ironclad-Tenement",
    }
    a = parse_activity_row(raw)
    assert a.type == "TRADE"
    assert a.condition_id == "0xcond"
    assert a.price == 0.62
    assert a.outcome_index == 0


def test_parse_activity_row_tolerates_redeem_with_int_price() -> None:
    # REDEEM rows had `price: int` in the discovery probe — coerce to float.
    raw = {
        "proxyWallet": "0xabc",
        "timestamp": 1715000000,
        "conditionId": "0xcond",
        "type": "REDEEM",
        "size": 100.0,
        "usdcSize": 100.0,
        "transactionHash": "0xtx2",
        "price": 1,
        "asset": "",
        "side": "",
        "outcomeIndex": 0,
        "title": "",
        "slug": "",
        "icon": "",
        "eventSlug": "",
        "outcome": "",
    }
    a = parse_activity_row(raw)
    assert a.type == "REDEEM"
    assert isinstance(a.price, float)
    assert a.price == 1.0


def test_parse_market_row_decodes_string_arrays() -> None:
    """Gamma returns outcomes/outcomePrices as JSON-encoded strings."""
    raw = {
        "id": "540817",
        "conditionId": "0xcond",
        "slug": "new-rhianna-album-before-gta-vi-926",
        "question": "New Rihanna Album before GTA VI?",
        "endDate": "2026-07-31T12:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.65", "0.35"]',
        "volumeNum": 716676.62,
        "liquidityNum": 44930.56,
        "closed": False,
        "active": True,
        "lastTradePrice": 0.65,
        "bestBid": 0.64,
        "bestAsk": 0.66,
    }
    m = parse_market_row(raw)
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.65, 0.35]
    assert m.volume_usdc == 716676.62
    assert m.closed is False


def test_parse_market_row_handles_resolved_with_category() -> None:
    raw = {
        "id": "12",
        "conditionId": "0xcond2",
        "slug": "trump-2020",
        "question": "Will Trump win 2020?",
        "category": "US-current-affairs",
        "endDate": "2020-11-04T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
        "closed": True,
        "active": False,
    }
    m = parse_market_row(raw)
    assert m.category == "US-current-affairs"
    assert m.closed is True
    assert m.outcome_prices == [0.0, 1.0]


def test_paginate_activity_walks_until_empty_page() -> None:
    pages = [
        [_activity_row(ts=1000, tx="0xa"), _activity_row(ts=999, tx="0xb")],
        [_activity_row(ts=998, tx="0xc")],
        [],  # exhausted
    ]
    call_count = {"n": 0}
    ends: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("start") is not None:
            return httpx.Response(200, json=[])
        ends.append(request.url.params.get("end"))
        body = pages[call_count["n"]]
        call_count["n"] += 1
        return httpx.Response(200, json=body)

    client = _client_with_handler(handler)
    result = _paginate_activity(client, "0xabc", page_size=2, max_pages=10, since_timestamp=None)
    assert [a.transaction_hash for a in result] == ["0xa", "0xb", "0xc"]
    assert ends == [None, "998"]
    client.close()


def test_paginate_activity_stops_at_watermark() -> None:
    """When a page contains rows older than since_timestamp, we stop after that page."""
    pages = [
        [_activity_row(ts=1000, tx="0xa"), _activity_row(ts=999, tx="0xb")],
        [_activity_row(ts=998, tx="0xc"), _activity_row(ts=900, tx="0xd")],  # 900 <= 950
        [_activity_row(ts=800, tx="0xe")],  # should not be fetched
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("start") is not None:
            return httpx.Response(200, json=[])
        if call_count["n"] >= len(pages):
            return httpx.Response(200, json=[])
        body = pages[call_count["n"]]
        call_count["n"] += 1
        return httpx.Response(200, json=body)

    client = _client_with_handler(handler)
    result = _paginate_activity(client, "0xabc", page_size=2, max_pages=10, since_timestamp=950)
    # Only events with ts > 950 should be returned, and the third page should never be fetched.
    assert sorted(a.transaction_hash for a in result) == ["0xa", "0xb", "0xc"]
    assert call_count["n"] == 2
    client.close()


def test_paginate_activity_respects_max_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("start") is not None:
            return httpx.Response(200, json=[])
        end = int(request.url.params.get("end") or 1000)
        return httpx.Response(200, json=[
            _activity_row(ts=end, tx=f"0x{end}"),
            _activity_row(ts=end - 1, tx=f"0x{end - 1}"),
        ])

    client = _client_with_handler(handler)
    result = _paginate_activity(client, "0xabc", page_size=2, max_pages=3, since_timestamp=None)
    assert len(result) == 6  # 3 pages × 2 rows
    client.close()


def test_paginate_activity_exhausts_same_second_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start")
        end = request.url.params.get("end")
        offset = int(request.url.params.get("offset") or 0)
        if start == "999" and end == "999":
            if offset == 0:
                return httpx.Response(200, json=[
                    _activity_row(ts=999, tx="0xb"),
                    _activity_row(ts=999, tx="0xc"),
                ])
            return httpx.Response(200, json=[_activity_row(ts=999, tx="0xd")])
        if end == "998":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[
            _activity_row(ts=1000, tx="0xa"),
            _activity_row(ts=999, tx="0xb"),
        ])

    client = _client_with_handler(handler)
    result = _paginate_activity_window(
        client, "0xabc", page_size=2, max_pages=5, since_timestamp=None
    )
    client.close()
    assert result.boundary_complete is True
    assert result.exhausted is True
    assert {event.transaction_hash for event in result.events} == {"0xa", "0xb", "0xc", "0xd"}


def test_paginate_activity_resumes_from_oldest_timestamp_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("start") is not None:
            return httpx.Response(200, json=[])
        end = int(request.url.params.get("end") or 5)
        return httpx.Response(200, json=[
            _activity_row(ts=end, tx=f"0x{end}"),
            _activity_row(ts=end - 1, tx=f"0x{end - 1}"),
        ])

    client = _client_with_handler(handler)
    first = _paginate_activity_window(
        client, "0xabc", page_size=2, max_pages=1, since_timestamp=None
    )
    second = _paginate_activity_window(
        client,
        "0xabc",
        page_size=2,
        max_pages=1,
        since_timestamp=None,
        end_timestamp=(first.oldest_timestamp or 0) - 1,
    )
    client.close()
    assert {event.timestamp for event in first.events} == {5, 4}
    assert {event.timestamp for event in second.events} == {3, 2}


def test_paginate_activity_window_streaming_matches_accumulate() -> None:
    """The on_page sink receives exactly the deduped events the accumulate path
    returns — including same-second boundary re-fetches — but holds nothing in
    memory (result.events stays empty). This pins the streaming refactor that
    keeps whale hydration from ballooning RAM."""
    def handler(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start")
        end = request.url.params.get("end")
        offset = int(request.url.params.get("offset") or 0)
        if start == "999" and end == "999":
            # Same-second boundary sweep: one overlapping row (0xb) plus a new
            # one (0xd) at offset 0, then exhausted.
            if offset == 0:
                return httpx.Response(200, json=[
                    _activity_row(ts=999, tx="0xb"),
                    _activity_row(ts=999, tx="0xd"),
                ])
            return httpx.Response(200, json=[])
        if end == "998":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[
            _activity_row(ts=1000, tx="0xa"),
            _activity_row(ts=999, tx="0xb"),
        ])

    client = _client_with_handler(handler)
    accumulate = _paginate_activity_window(
        client, "0xabc", page_size=2, max_pages=5, since_timestamp=None
    )
    streamed: list[PolymarketActivity] = []
    result = _paginate_activity_window(
        client,
        "0xabc",
        page_size=2,
        max_pages=5,
        since_timestamp=None,
        on_page=streamed.extend,
    )
    client.close()

    # Streaming keeps no per-wallet accumulation.
    assert result.events == []
    # Same termination metadata either way.
    assert (result.exhausted, result.boundary_complete, result.oldest_timestamp) == (
        accumulate.exhausted,
        accumulate.boundary_complete,
        accumulate.oldest_timestamp,
    )
    # The sink sees the same events after the store's dedupe key collapses the
    # overlapping boundary re-fetch (0xb appears on both the page and the sweep).
    assert {_activity_key(e) for e in streamed} == {
        _activity_key(e) for e in accumulate.events
    }
    assert {e.transaction_hash for e in accumulate.events} == {"0xa", "0xb", "0xd"}


def test_paginate_positions_walks_beyond_first_page() -> None:
    offsets: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params.get("offset"))
        offset = int(request.url.params.get("offset") or 0)
        count = 500 if offset == 0 else 1
        return httpx.Response(200, json=[
            {"conditionId": f"0x{offset + i}", "outcomeIndex": 0}
            for i in range(count)
        ])

    client = _client_with_handler(handler)
    rows = _paginate_positions(client, "0xabc")
    client.close()
    assert len(rows) == 501
    assert offsets == [None, "500"]


def test_markets_backfill_fetches_closed_and_active_metadata(tmp_path: Path) -> None:
    closed_values: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        closed = request.url.params.get("closed")
        closed_values.append(str(closed))
        if closed == "true":
            return httpx.Response(200, json=[{
                "id": "1", "conditionId": "0xc", "slug": "settled", "question": "?",
                "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
                "closed": True, "active": False,
            }])
        return httpx.Response(200, json=[])

    stores = _build_stores(tmp_path)
    stores.activity.write_activity([parse_activity_row(_activity_row(1000, "0xa"))])
    client = _client_with_handler(handler)
    written = run_markets_backfill_from_activity(client, stores)
    client.close()
    assert written == 1
    assert closed_values == ["true", "false"]


def test_markets_backfill_skips_settled_markets(tmp_path: Path) -> None:
    gamma_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gamma_calls.append(request.url.host)
        return httpx.Response(200, json=[])

    stores = _build_stores(tmp_path)
    stores.activity.write_activity([parse_activity_row(_activity_row(1000, "0xa"))])
    settled = parse_market_row({
        "id": "1",
        "conditionId": "0xc",
        "slug": "settled",
        "question": "?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "closed": True,
        "active": False,
    })
    stores.markets.write_markets([settled])

    client = _client_with_handler(handler)
    written = run_markets_backfill_from_activity(client, stores, open_market_ttl_seconds=3600)
    client.close()

    assert written == 0
    assert gamma_calls == []


def test_markets_backfill_refreshes_stale_open_markets_only(tmp_path: Path) -> None:
    closed_values: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        closed_values.append(request.url.params.get("closed"))
        return httpx.Response(200, json=[{
            "id": "2", "conditionId": "0xc", "slug": "open", "question": "?",
            "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "0.4"]',
            "closed": False, "active": True,
        }])

    stores = _build_stores(tmp_path)
    stores.activity.write_activity([parse_activity_row(_activity_row(1000, "0xa"))])
    stale = parse_market_row({
        "id": "1",
        "conditionId": "0xc",
        "slug": "open",
        "question": "?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        "closed": False,
        "active": True,
    })
    object.__setattr__(stale, "fetched_at", "2020-01-01T00:00:00Z")
    stores.markets.write_markets([stale])

    client = _client_with_handler(handler)
    written = run_markets_backfill_from_activity(client, stores, open_market_ttl_seconds=3600)
    client.close()

    assert written == 2
    assert closed_values == ["true", "false"]


def test_plan_market_backfill_splits_missing_and_stale() -> None:
    missing, stale = _plan_market_backfill_fetches(
        {"0xnew", "0xopen"},
        {
            "0xopen": {"closed": False, "fetched_at": "2020-01-01T00:00:00Z"},
            "0xsettled": {"closed": True, "fetched_at": "2026-01-01T00:00:00Z"},
        },
        open_market_ttl_seconds=3600,
    )
    assert missing == ["0xnew"]
    assert stale == ["0xopen"]


def test_run_wallets_uses_economics_cache_without_user_leaderboard_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import marketsignalos_polymarket.runner as runner_module

    cache_path = tmp_path / "polymarket_wallet_economics_cache.json"
    monkeypatch.setattr(runner_module, "_economics_cache_path", lambda: cache_path)
    _write_economics_cache({
        "0xabc": {
            "ALL": _EconomicsPeriodEntry(pnl_usdc=100.0, volume_usdc=1000.0),
            "MONTH": _EconomicsPeriodEntry(pnl_usdc=10.0, volume_usdc=None),
        },
    })

    leaderboard_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/leaderboard" and request.url.params.get("user"):
            leaderboard_calls.append(str(request.url.params.get("user")))
        if path == "/activity":
            return httpx.Response(200, json=[])
        if path == "/positions":
            return httpx.Response(200, json=[])
        if path == "/closed-positions":
            return httpx.Response(200, json=[])
        if path == "/value":
            return httpx.Response(200, json=[{"user": "0xabc", "value": 1.0}])
        if path.startswith("/user-pnl"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    run_wallets(client, stores, addresses=["0xabc"], max_pages_per_wallet=1)
    client.close()

    assert leaderboard_calls == []
    hydration = stores.hydration.load_hydration()["0xabc"]
    assert hydration.economic_all_time_complete is True
    assert hydration.economic_month_complete is True
    assert hydration.all_time_pnl_usdc == 100.0
    assert hydration.pnl_30d_usdc == 10.0


def test_select_shallow_wallet_targets_prefers_hot_wallets(tmp_path: Path) -> None:
    stores = _build_stores(tmp_path)
    enrichment = stores.activity_path.parent / "polymarket_wallet_enrichment.jsonl"
    enrichment.write_text(
        json.dumps({
            "proxy_wallet": "0xhot",
            "skill_likelihood": 0.95,
            "resolved_trades": 40,
            "tailability_status": "tailable",
        }) + "\n",
        encoding="utf-8",
    )
    selected = _select_shallow_wallet_targets(
        ["0xhot", "0xcold"],
        stores=stores,
    )
    assert selected == ["0xhot"]


def test_run_wallets_advances_checkpoint(tmp_path: Path) -> None:
    pages = [
        [_activity_row(ts=2000, tx="0xa"), _activity_row(ts=1500, tx="0xb")],
        [],
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/activity":
            body = pages[min(call_count["n"], len(pages) - 1)]
            call_count["n"] += 1
            return httpx.Response(200, json=body)
        if path == "/positions":
            return httpx.Response(200, json=[])
        if path == "/value":
            return httpx.Response(200, json=[{"user": "0xabc", "value": 1234.5}])
        return httpx.Response(404)

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    run_wallets(client, stores, addresses=["0xabc"], activity_page_size=2, max_pages_per_wallet=5)

    assert stores.checkpoints.get_last_timestamp("0xabc") == 2000

    # Second run should fetch nothing new (page0 still returns ts <= 2000, so paginator stops).
    call_count["n"] = 0
    written_a, _, _ = run_wallets(
        client, stores, addresses=["0xabc"], activity_page_size=2, max_pages_per_wallet=5
    )
    # All rows in page0 have ts ∈ {2000, 1500} — neither is > 2000, so nothing new is collected.
    assert written_a == 0
    client.close()


def test_run_wallets_emits_progress_per_wallet(tmp_path: Path) -> None:
    """The progress callback should fire once per wallet with current/total
    so the API can surface wallet-scan progress to the UI."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/activity":
            return httpx.Response(200, json=[])
        if request.url.path == "/positions":
            return httpx.Response(200, json=[])
        if request.url.path == "/value":
            return httpx.Response(200, json=[{"user": "0x", "value": 0.0}])
        return httpx.Response(404)

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    events: list[dict[str, Any]] = []
    addresses = ["0xaaa", "0xbbb", "0xccc"]
    run_wallets(
        client, stores, addresses=addresses,
        activity_page_size=10, max_pages_per_wallet=1,
        progress_cb=events.append,
    )
    client.close()

    assert len(events) == 3
    assert [e["current"] for e in events] == [1, 2, 3]
    assert all(e["total"] == 3 for e in events)
    assert [e["wallet"] for e in events] == addresses
    assert all(e["stage"] == "wallets" for e in events)


def test_seed_watchlist_skips_api_when_limit_is_zero(tmp_path: Path) -> None:
    """N<=0 means 'skip this side'. The leaderboard API treats limit=0 as
    'no limit' and returns its default page (~50), so the guard must be
    client-side: skip the GET entirely."""
    watchlist = tmp_path / "wl.txt"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        metric = request.url.path.lstrip("/")
        calls.append(metric)
        if metric == "profit":
            return httpx.Response(200, json=[
                {"proxyWallet": "0xProfit1", "amount": 1.0, "pseudonym": "p", "name": "p"},
            ])
        # volume must never be called for this test.
        return httpx.Response(200, json=[
            {"proxyWallet": "0xShouldNotAppear", "amount": 9, "pseudonym": "x", "name": "x"},
        ])

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    merged = seed_watchlist_from_leaderboard(
        client, stores, watchlist, top_n_profit=1, top_n_volume=0
    )
    assert calls == ["profit"], f"volume API should have been skipped, got: {calls}"
    assert merged == ["0xprofit1"]
    client.close()


def test_seed_watchlist_skips_both_when_both_zero(tmp_path: Path) -> None:
    """Pathological but legal: N=0 on both sides — no HTTP calls at all."""
    watchlist = tmp_path / "wl.txt"
    watchlist.write_text("# manual\n0xkeep\n", encoding="utf-8")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)  # would fail if hit

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    merged = seed_watchlist_from_leaderboard(
        client, stores, watchlist, top_n_profit=0, top_n_volume=0
    )
    assert calls == [], f"no API calls expected, got: {calls}"
    # Existing manual wallets preserved even when both sides are skipped.
    assert merged == ["0xkeep"]
    client.close()


def test_seed_watchlist_merges_with_existing(tmp_path: Path) -> None:
    """seed-watchlist preserves manually added wallets and adds top traders."""
    watchlist = tmp_path / "wl.txt"
    watchlist.write_text("# manual\n0xmanual1\n0xmanual2\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        metric = request.url.path.lstrip("/")
        if metric == "profit":
            return httpx.Response(200, json=[
                {"proxyWallet": "0xProfit1", "amount": 1.0, "pseudonym": "p1", "name": "p1"},
                {"proxyWallet": "0xmanual1", "amount": 0.5, "pseudonym": "m1", "name": "m1"},  # overlap
            ])
        if metric == "volume":
            return httpx.Response(200, json=[
                {"proxyWallet": "0xVolume1", "amount": 2.0, "pseudonym": "v1", "name": "v1"},
            ])
        return httpx.Response(404)

    client = _client_with_handler(handler)
    stores = _build_stores(tmp_path)
    merged = seed_watchlist_from_leaderboard(
        client, stores, watchlist, top_n_profit=2, top_n_volume=1
    )
    # Wallets are lowercased and deduped.
    assert "0xprofit1" in merged
    assert "0xvolume1" in merged
    assert "0xmanual1" in merged
    assert "0xmanual2" in merged
    assert len(merged) == 4

    contents = watchlist.read_text(encoding="utf-8")
    assert "0xprofit1" in contents
    assert "0xmanual2" in contents  # manual entry preserved
    client.close()


def test_is_kalshi_parlay_detects_kxmve_prefix() -> None:
    """KXMVE* tickers are multi-game parlays whose concatenated leg titles
    pollute the TF-IDF matcher."""
    assert _is_kalshi_parlay("KXMVESPORTSMULTIGAMEEXTENDED-S20264AC9FA0176D-8E5864C593E")
    assert _is_kalshi_parlay("KXMVE-ANYTHING")
    # case-insensitive: ticker casing varies across Kalshi feeds.
    assert _is_kalshi_parlay("kxmve-sports-foo")
    # Normal Kalshi tickers must not be filtered.
    assert not _is_kalshi_parlay("KXFEDDECISION-25SEP-50BP")
    assert not _is_kalshi_parlay("KXNFLGAME-2026MAYJETS")
    # Empty / pathological inputs.
    assert not _is_kalshi_parlay("")


def _kalshi_market(
    ticker: str, title: str, *, end: str = "2026-06-01T00:00:00Z"
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker, event_ticker="EV", title=title, subtitle="", yes_sub_title="",
        category="Sports", status="open", expiration_time=end, close_time=end,
        yes_bid=50, yes_ask=52, last_price=51,
    )


def _poly_market(condition_id: str, question: str, *, end: str = "2026-06-01T00:00:00Z") -> PolymarketMarket:
    return PolymarketMarket(
        gamma_id="g1", condition_id=condition_id, slug="s",
        question=question, category="Sports", end_date=end,
        outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
        volume_usdc=1000, liquidity_usdc=500, closed=False, active=True,
        last_trade_price=0.5, best_bid=0.49, best_ask=0.51,
    )


def test_run_match_markets_excludes_kalshi_parlays(tmp_path: Path) -> None:
    """End-to-end through run_match_markets: a parlay ticker whose title would
    otherwise be a strong TF-IDF match against a Polymarket question must not
    appear in the produced market_links."""
    stores = _build_stores(tmp_path)

    # Polymarket side: one straight market about a Yankees vs Red Sox game.
    poly_q = "Will the Yankees beat the Red Sox on June 1?"
    poly_markets = [_poly_market("0xstraight", poly_q)]
    # Persist via the JSONL store so run_match_markets's loader can read it back.
    stores.markets.write_markets(poly_markets)

    # Kalshi side: one parlay whose concatenated title contains the same teams
    # (would TF-IDF-match the Polymarket question) PLUS one legitimate single-event
    # market that should match.
    parlay = _kalshi_market(
        "KXMVESPORTSMULTIGAMEEXTENDED-2026JUN-LEGS",
        "yes Yankees Red Sox, yes Mets Phillies, yes Dodgers Padres",
    )
    legit = _kalshi_market(
        "KXMLBYANKEESREDSOX-2026JUN01",
        "Will the Yankees beat the Red Sox on June 1?",
    )
    write_kalshi_markets_jsonl([parlay, legit], stores.kalshi_markets_path)

    run_match_markets(stores)

    links = stores.market_links.load_links()
    tickers = {link.kalshi_ticker for link in links}
    assert parlay.ticker not in tickers, \
        f"parlay should be excluded, but appeared in: {tickers}"
    assert legit.ticker in tickers, \
        f"legit market should still match, links: {tickers}"


def test_parse_position_row_falls_back_on_alt_field_names() -> None:
    raw = {
        "conditionId": "0xc",
        "outcomeIndex": 1,
        "outcome": "No",
        "size": 250.0,
        "averagePrice": 0.42,  # alt name
        "value": 95.0,         # alt name
        "slug": "s",
        "title": "t",
        "eventSlug": "e",
    }
    p = parse_position_row(raw, proxy_wallet="0xABC")
    assert p.proxy_wallet == "0xabc"
    assert p.avg_price == 0.42
    assert p.current_value_usdc == 95.0


def test_run_pipeline_skips_windows_the_api_rejects(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """Polymarket's public leaderboard API silently 400s on some window
    values. The orchestrator must catch those, log a warning, and still
    complete the rest of the pipeline."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Leaderboard: accept 'all', reject everything else with 400.
        if "lb-api.polymarket.com" in str(request.url):
            window = request.url.params.get("window")
            if window == "all":
                return httpx.Response(200, json=[])
            return httpx.Response(400, json={"error": "invalid request"})
        # Gamma markets: return an empty page so run_markets exits cleanly.
        if "gamma-api.polymarket.com" in str(request.url):
            return httpx.Response(200, json=[])
        # Data API (activity / positions / value) — none of these should be
        # called since the watchlist will be empty, but cover them anyway.
        return httpx.Response(200, json=[])

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))

    result = run_pipeline(
        windows=["day", "week", "month", "all"],
        leaderboard_limit=10,
        skip_kalshi=True,
        client=_client_with_handler(handler),
    )

    # Only 'all' returned successfully; the other three were skipped.
    assert result.windows_attempted == ["day", "week", "month", "all"]
    assert result.windows_succeeded == ["all"]
    # Empty leaderboard => no wallets seeded, no activity pulled.
    assert result.wallets_seeded == 0
    assert result.leaderboard_entries == 0
    # Markets fetch returned an empty page, but the pipeline didn't crash.
    assert result.markets_written == 0
    assert result.enrichment_wallets == 0
    # Kalshi skipped per the flag.
    assert result.kalshi_markets == 0
    assert result.market_links == 0


def test_run_pipeline_seeds_volume_only_by_default(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The shallow pipeline must drop the profit/PnL leaderboard (luck bias)
    and must NOT touch the subgraph — recent-trader discovery lives only in the
    deep pipeline, which can absorb an unbounded wallet set."""
    metrics_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lb-api.polymarket.com" in url:
            metrics_called.append(request.url.path.lstrip("/"))
            return httpx.Response(200, json=[
                {"proxyWallet": "0xVol", "amount": 1.0, "pseudonym": "v", "name": "v"},
            ])
        if "gamma-api.polymarket.com" in url:
            return httpx.Response(200, json=[])
        if "api.goldsky.com" in url:
            raise AssertionError("shallow pipeline must not call the subgraph")
        return httpx.Response(200, json=[])

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    result = run_pipeline(
        windows=["all"], leaderboard_limit=5, skip_kalshi=True,
        client=_client_with_handler(handler),
    )
    assert metrics_called == ["volume"]  # profit never requested
    assert result.wallets_seeded == 1


def test_run_pipeline_include_profit_reenables_profit(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    metrics_called: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lb-api.polymarket.com" in url:
            metrics_called.add(request.url.path.lstrip("/"))
            return httpx.Response(200, json=[])
        if "gamma-api.polymarket.com" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    run_pipeline(
        windows=["all"], leaderboard_limit=5, skip_kalshi=True,
        include_profit_leaderboard=True, client=_client_with_handler(handler),
    )
    assert metrics_called == {"profit", "volume"}


def test_merge_skill_qualified_wallets_into_watchlist(tmp_path: Path) -> None:
    from marketsignalos_polymarket.runner import _merge_skill_qualified_wallets_into_watchlist

    enrichment = tmp_path / "polymarket_wallet_enrichment.jsonl"
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("# header\n0xexisting\n", encoding="utf-8")
    enrichment.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "proxy_wallet": "0xexisting",
                        "skill_likelihood": 0.95,
                        "resolved_trades": 40,
                        "tailability_status": "tailable",
                    },
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        "proxy_wallet": "0xedge",
                        "skill_likelihood": 0.7,
                        "resolved_trades": 15,
                        "tailability_status": "blocked",
                    },
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        "proxy_wallet": "0xweak",
                        "skill_likelihood": 0.4,
                        "resolved_trades": 30,
                        "tailability_status": "blocked",
                    },
                    separators=(",", ":"),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    added = _merge_skill_qualified_wallets_into_watchlist(
        enrichment_path=enrichment,
        watchlist_path=watchlist,
    )
    assert added == 1
    wallets = {
        line.strip()
        for line in watchlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert wallets == {"0xedge", "0xexisting"}


# ── Manual watchlist additions ───────────────────────────────────────────────

def test_add_watchlist_wallet_appends_pins_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marketsignalos_polymarket.runner import (
        run_add_watchlist_wallet,
        run_list_watchlist,
    )

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    address = "0x" + "AbCd" * 10  # mixed case — must normalize to lowercase

    result = run_add_watchlist_wallet(address)
    assert result["added"] is True
    assert result["wallet"] == address.lower()
    assert result["review_status"] == "pinned"
    assert run_list_watchlist() == [address.lower()]

    # Idempotent: a second add only re-asserts the pin.
    again = run_add_watchlist_wallet(address.lower())
    assert again["added"] is False
    assert again["watchlist_size"] == 1
    assert run_list_watchlist() == [address.lower()]

    # The file keeps its header comment so seeding passes recognize it.
    content = (tmp_path / "wl.txt").read_text(encoding="utf-8")
    assert content.startswith("# Polymarket wallet watchlist")


def test_add_watchlist_wallet_preserves_existing_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marketsignalos_polymarket.runner import (
        run_add_watchlist_wallet,
        run_list_watchlist,
    )

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    wl = tmp_path / "wl.txt"
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(wl))
    existing = "0x" + "1" * 40
    wl.write_text(f"# header\n{existing}\n", encoding="utf-8")

    new = "0x" + "2" * 40
    run_add_watchlist_wallet(new)
    assert run_list_watchlist() == [existing, new]


def test_add_watchlist_wallet_rejects_invalid_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marketsignalos_polymarket.runner import run_add_watchlist_wallet

    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_WATCHLIST_PATH", str(tmp_path / "wl.txt"))
    for bad in ("", "AlphaWhale", "0x123", "0x" + "g" * 40, "1x" + "a" * 40):
        with pytest.raises(ValueError):
            run_add_watchlist_wallet(bad)
    assert not (tmp_path / "wl.txt").exists()


# ── Activity sharding (streaming enrichment) ─────────────────────────────────


def test_shard_activity_by_wallet_partitions_and_preserves_order(tmp_path: Path) -> None:
    from marketsignalos_polymarket.runner import (
        _iter_activity_shards,
        _shard_activity_by_wallet,
    )

    rows = [
        {
            "proxy_wallet": f"0xw{i % 3}", "timestamp": i, "condition_id": f"0xc{i}",
            "type": "TRADE", "side": "BUY", "size": 1.0, "usdc_size": 0.5,
            "price": 0.5, "outcome_index": 0, "outcome": "Yes", "slug": "s",
            "title": "t", "event_slug": "e", "transaction_hash": f"0xtx{i}",
        }
        for i in range(10)
    ]
    src = tmp_path / "activity.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    shard_paths = _shard_activity_by_wallet(
        src, shard_count=4, tmp_dir=tmp_path / "shards"
    )
    assert len(shard_paths) == 4

    shard_of_wallet: dict[str, int] = {}
    timestamps_by_wallet: dict[str, list[int]] = {}
    total = 0
    for index, shard in enumerate(_iter_activity_shards(shard_paths)):
        for event in shard:
            total += 1
            # Wallet-disjoint: every event of a wallet lands in one shard.
            assert shard_of_wallet.setdefault(event.proxy_wallet, index) == index
            timestamps_by_wallet.setdefault(event.proxy_wallet, []).append(
                event.timestamp
            )
    assert total == 10
    # File order preserved within each wallet.
    for stamps in timestamps_by_wallet.values():
        assert stamps == sorted(stamps)
