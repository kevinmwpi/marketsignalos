"""
External deep-links for tail-the-whale UX: clickable destinations on
Polymarket and Kalshi corresponding to the signals we surface.

All builders return an empty string when their input is missing/blank, so
clients can use string truthiness to decide whether to render a link.
We never guess identifiers: if the upstream record doesn't carry an
event slug / ticker, the link is omitted rather than constructed from
something close-but-wrong (e.g. a market slug or a ticker prefix), which
would land users on a 404 or, worse, the wrong market.

Verified shapes:
  - https://polymarket.com/profile/<lowercase-hex-wallet>
  - https://polymarket.com/event/<event_slug>           (NOT market slug)
  - https://kalshi.com/markets/<lowercase event_ticker> (NOT first dash segment)
"""
from __future__ import annotations

_POLYMARKET_BASE = "https://polymarket.com"
_KALSHI_BASE = "https://kalshi.com"


def polymarket_profile_url(wallet: str) -> str:
    """Profile / activity page for a Polymarket proxy wallet."""
    if not wallet:
        return ""
    return f"{_POLYMARKET_BASE}/profile/{wallet.lower()}"


def polymarket_market_url(event_slug: str) -> str:
    """
    Polymarket event page. Pass the event_slug carried on Polymarket
    position/market records — NOT the per-market slug, which often differs
    (e.g. position.slug='fed-50bp-sep' but event_slug='fed-decision') and
    would 404 on /event/.
    """
    event_slug = event_slug.strip()
    if not event_slug:
        return ""
    return f"{_POLYMARKET_BASE}/event/{event_slug}"


def kalshi_market_url(event_ticker: str) -> str:
    """
    Kalshi event page. Pass the event_ticker carried on the Kalshi market
    record (e.g. 'KXFED-26SEP') — NOT the per-market ticker
    ('KXFED-26SEP-50BP'). We deliberately don't try to derive the event
    ticker by splitting the market ticker on '-': Kalshi event tickers can
    themselves contain dashes (e.g. 'KXFED-26SEP'), so a naive split
    silently lands on the wrong page.
    """
    event_ticker = event_ticker.strip()
    if not event_ticker:
        return ""
    return f"{_KALSHI_BASE}/markets/{event_ticker.lower()}"
