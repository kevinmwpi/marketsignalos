from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    """Canonical trade payload used across prediction-market providers."""

    source: str
    market_ticker: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


@dataclass(frozen=True, slots=True)
class NormalizedFill:
    """Account-level fill payload for suspicious-account scoring."""

    source: str
    account_id: str
    market_ticker: str
    fill_id: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolved market outcome used to score wins/losses."""

    source: str
    market_ticker: str
    resolved_at: str
    settlement_side: str
