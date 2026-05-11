"""
Discovers active Kalshi market tickers by paging through the markets API.

Used by the runner when INGEST_AUTO_DISCOVER=1, replacing the manual
INGEST_MARKET_TICKERS env var. Filters by open_interest and volume
so only markets with actual trading activity are returned.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .kalshi_client import KalshiClient

log = logging.getLogger("marketsignalos.discovery")

# Markets to always include regardless of volume (high-signal categories).
_HIGH_SIGNAL_SERIES = {
    "KXBTCD",    # Bitcoin daily price
    "KXETHD",    # Ethereum daily price
    "KXFED",     # Fed funds rate decisions
    "KXINFLATION",  # CPI / inflation
    "KXUNEMPLOY",   # US unemployment
    "KXGDP",     # GDP
    "KXPRES",    # Presidential approval / elections
    "KXCONGRESS",   # Congressional races
}


def discover_active_tickers(
    client: "KalshiClient",
    *,
    max_tickers: int = 500,
    min_open_interest: float = 0.0,
    include_series: set[str] | None = None,
) -> list[str]:
    """
    Page through open markets and return tickers ordered by open interest (desc).

    Args:
        client: Authenticated KalshiClient instance.
        max_tickers: Hard cap on returned tickers (prevents runaway pagination).
        min_open_interest: Skip markets below this open-interest floor.
        include_series: Series prefixes to always include (overrides min_open_interest).

    Returns:
        List of market tickers sorted by open_interest descending.
    """
    must_include = include_series if include_series is not None else _HIGH_SIGNAL_SERIES

    collected: list[tuple[float, str]] = []  # (open_interest, ticker)
    cursor: str | None = None
    pages = 0
    max_pages = 50  # safety cap: 50 pages × 200 markets = 10 000 max

    while pages < max_pages:
        payload = client.list_markets(status="open", limit=200, cursor=cursor)
        markets = payload.get("markets", [])
        if not isinstance(markets, list):
            break

        for market in markets:
            if not isinstance(market, dict):
                continue
            ticker = str(market.get("ticker", ""))
            oi_raw = market.get("open_interest_fp", "0")
            try:
                oi = float(oi_raw)
            except (TypeError, ValueError):
                oi = 0.0

            series = ticker.split("-")[0]
            qualifies = oi >= min_open_interest or series in must_include
            if qualifies and ticker:
                collected.append((oi, ticker))

        next_cursor = payload.get("cursor")
        pages += 1

        if not markets or not next_cursor:
            break
        cursor = str(next_cursor)

    collected.sort(key=lambda x: x[0], reverse=True)
    tickers = [t for _, t in collected[:max_tickers]]

    log.info(
        "discovery pages_fetched=%d candidates=%d returned=%d",
        pages,
        len(collected),
        len(tickers),
    )
    return tickers


def discover_settled_tickers(
    client: "KalshiClient",
    *,
    max_tickers: int = 500,
) -> list[str]:
    """Returns recently settled market tickers for historical resolution ingestion."""
    collected: list[str] = []
    cursor: str | None = None
    pages = 0

    while pages < 10 and len(collected) < max_tickers:
        payload = client.list_markets(status="settled", limit=200, cursor=cursor)
        markets = payload.get("markets", [])
        if not isinstance(markets, list):
            break

        for market in markets:
            if not isinstance(market, dict):
                continue
            ticker = str(market.get("ticker", ""))
            if ticker:
                collected.append(ticker)

        next_cursor = payload.get("cursor")
        pages += 1
        if not markets or not next_cursor or len(collected) >= max_tickers:
            break
        cursor = str(next_cursor)

    return collected[:max_tickers]
