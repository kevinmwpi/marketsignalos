"""
External deep-links for tail-the-whale UX: clickable destinations on
Polymarket and Kalshi corresponding to the signals we surface.

All builders return an empty string when their input is missing/blank, so
clients can use string truthiness to decide whether to render a link. The
URL formats are best-effort and centralized here so a vendor change is a
one-file edit.

Verified shapes (Polymarket + Kalshi as of 2026-05):
  - https://polymarket.com/profile/<lowercase-hex-wallet>
  - https://polymarket.com/event/<market-or-event-slug>
  - https://kalshi.com/markets/<lowercase-event-ticker>
"""
from __future__ import annotations

_POLYMARKET_BASE = "https://polymarket.com"
_KALSHI_BASE = "https://kalshi.com"


def polymarket_profile_url(wallet: str) -> str:
    """Profile / activity page for a Polymarket proxy wallet."""
    if not wallet:
        return ""
    return f"{_POLYMARKET_BASE}/profile/{wallet.lower()}"


def polymarket_market_url(slug: str) -> str:
    """
    Market or event page. Polymarket unifies binary markets and multi-outcome
    events under /event/<slug>; we don't try to distinguish the two — pass
    whichever slug the upstream data exposed.
    """
    slug = slug.strip()
    if not slug:
        return ""
    return f"{_POLYMARKET_BASE}/event/{slug}"


def kalshi_market_url(ticker: str) -> str:
    """
    Event page for a Kalshi market ticker. Kalshi tickers are
    `<event_ticker>-<expiry>-<strike>`; the /markets/<event_ticker> URL
    lands on the event with all sub-markets visible.
    """
    ticker = ticker.strip()
    if not ticker:
        return ""
    # Everything before the first dash is the event ticker.
    event_ticker = ticker.split("-", 1)[0]
    return f"{_KALSHI_BASE}/markets/{event_ticker.lower()}"
