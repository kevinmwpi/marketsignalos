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


def test_polymarket_market_url_uses_event_slug() -> None:
    """Polymarket /event/<slug> requires the EVENT slug (the parent), not the
    per-market slug. Picking the right input is the responsibility of the
    caller; this builder just routes the value into the URL."""
    assert (
        polymarket_market_url("2026-fifa-world-cup-winner-595")
        == "https://polymarket.com/event/2026-fifa-world-cup-winner-595"
    )


def test_polymarket_market_url_strips_whitespace() -> None:
    assert polymarket_market_url("  fed-decision  ") \
        == "https://polymarket.com/event/fed-decision"


def test_polymarket_market_url_empty_returns_empty_no_guessing() -> None:
    """If we don't have an event_slug we DO NOT fall back to the market slug
    (would 404), and we don't synthesize one — just omit the link."""
    assert polymarket_market_url("") == ""
    assert polymarket_market_url("   ") == ""


def test_kalshi_market_url_uses_full_event_ticker_including_dashes() -> None:
    """Kalshi event tickers themselves can contain dashes (e.g.
    'KXFED-26SEP'); the builder must take the whole event_ticker, not split
    it on the first dash and silently drop the suffix."""
    assert (
        kalshi_market_url("KXFED-26SEP")
        == "https://kalshi.com/markets/kxfed-26sep"
    )


def test_kalshi_market_url_lowercases() -> None:
    assert kalshi_market_url("KXNFLGAME") == "https://kalshi.com/markets/kxnflgame"


def test_kalshi_market_url_strips_whitespace() -> None:
    assert kalshi_market_url("  KXFED-26SEP ") == "https://kalshi.com/markets/kxfed-26sep"


def test_kalshi_market_url_empty_returns_empty() -> None:
    assert kalshi_market_url("") == ""
    assert kalshi_market_url("   ") == ""
