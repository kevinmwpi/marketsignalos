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
