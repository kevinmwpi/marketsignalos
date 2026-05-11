from __future__ import annotations

from marketsignalos_polymarket.runner import (
    parse_activity_row,
    parse_leaderboard_row,
    parse_market_row,
    parse_position_row,
)


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
