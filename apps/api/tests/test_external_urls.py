from __future__ import annotations

from marketsignalos_api.services.external_urls import (
    kalshi_market_url,
    polymarket_market_url,
    polymarket_profile_url,
)


def test_polymarket_profile_url_lowercases_wallet() -> None:
    """Polymarket profile URLs are case-sensitive in the path; normalize so
    mixed-case addresses from upstream sources resolve consistently."""
    assert polymarket_profile_url("0xABC123") == "https://polymarket.com/profile/0xabc123"


def test_polymarket_profile_url_empty_returns_empty() -> None:
    # Truthiness contract: empty string means "no link" so the panel can skip.
    assert polymarket_profile_url("") == ""


def test_polymarket_market_url_uses_event_route() -> None:
    assert (
        polymarket_market_url("fed-decision-sep-2026-50bp")
        == "https://polymarket.com/event/fed-decision-sep-2026-50bp"
    )


def test_polymarket_market_url_strips_whitespace() -> None:
    assert polymarket_market_url("  slug-with-padding  ") \
        == "https://polymarket.com/event/slug-with-padding"


def test_polymarket_market_url_empty_returns_empty() -> None:
    assert polymarket_market_url("") == ""
    assert polymarket_market_url("   ") == ""


def test_kalshi_market_url_strips_to_event_ticker() -> None:
    """A Kalshi market ticker is `<event_ticker>-<expiry>-<strike>`;
    only the event_ticker is needed for the deep link."""
    assert (
        kalshi_market_url("KXFEDDECISION-25SEP-50BP")
        == "https://kalshi.com/markets/kxfeddecision"
    )


def test_kalshi_market_url_handles_ticker_with_no_dash() -> None:
    # Some legacy tickers don't have the dash-separated structure.
    assert kalshi_market_url("KXSIMPLE") == "https://kalshi.com/markets/kxsimple"


def test_kalshi_market_url_empty_returns_empty() -> None:
    assert kalshi_market_url("") == ""
