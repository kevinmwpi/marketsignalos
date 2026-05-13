from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from marketsignalos_polymarket.polymarket_client import (
    PolymarketClient,
    PolymarketClientConfig,
)
from marketsignalos_polymarket.kalshi_markets_fetch import (
    KalshiMarket,
    write_kalshi_markets_jsonl,
)
from marketsignalos_polymarket.models import PolymarketMarket
from marketsignalos_polymarket.runner import (
    _build_stores,
    _is_kalshi_parlay,
    _paginate_activity,
    parse_activity_row,
    parse_leaderboard_row,
    parse_market_row,
    parse_position_row,
    run_match_markets,
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

    def handler(request: httpx.Request) -> httpx.Response:
        expected_offset = call_count["n"] * 2
        actual = request.url.params.get("offset")
        # Client omits offset=0 from the query string; offsets > 0 are sent.
        if expected_offset == 0:
            assert actual is None
        else:
            assert actual == str(expected_offset)
        body = pages[call_count["n"]]
        call_count["n"] += 1
        return httpx.Response(200, json=body)

    client = _client_with_handler(handler)
    result = _paginate_activity(client, "0xabc", page_size=2, max_pages=10, since_timestamp=None)
    assert [a.transaction_hash for a in result] == ["0xa", "0xb", "0xc"]
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
        # Always return a full page so the only stop condition is max_pages.
        return httpx.Response(200, json=[_activity_row(ts=1000, tx="0xa"),
                                         _activity_row(ts=999, tx="0xb")])

    client = _client_with_handler(handler)
    result = _paginate_activity(client, "0xabc", page_size=2, max_pages=3, since_timestamp=None)
    assert len(result) == 6  # 3 pages × 2 rows
    client.close()


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
