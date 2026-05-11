"""
Polymarket data models.

Field names are derived from the actual API responses captured in Phase 0
(see docs/polymarket-api-discovery.md). All wallet addresses are normalized
to lowercase hex; conditionIds keep their 0x-prefixed canonical form.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PolymarketLeaderboardEntry:
    """One row from lb-api.polymarket.com/profit or /volume."""

    proxy_wallet: str
    name: str
    pseudonym: str
    amount_usdc: float
    metric: str  # "profit" | "volume"
    window: str  # "all" | "1d" | "1w" | "1m" (whatever the API accepts)
    fetched_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketActivity:
    """
    One event from data-api.polymarket.com/activity?user=<addr>.

    type=TRADE rows are buys/sells; type=REDEEM rows mark settlement of a
    resolved market and are how we tell a position closed in the money.
    """

    proxy_wallet: str
    timestamp: int  # unix seconds
    condition_id: str
    type: str  # "TRADE" | "REDEEM" | other
    side: str  # "BUY" | "SELL" | "" for redeem
    size: float
    usdc_size: float
    price: float
    outcome_index: int
    outcome: str  # "Yes" | "No" | ""
    slug: str
    title: str
    event_slug: str
    transaction_hash: str
    name: str = ""
    pseudonym: str = ""
    fetched_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketPosition:
    """A currently-open position from data-api.polymarket.com/positions."""

    proxy_wallet: str
    condition_id: str
    outcome_index: int
    outcome: str
    size: float
    avg_price: float
    current_value_usdc: float
    slug: str
    title: str
    event_slug: str
    snapshot_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketMarket:
    """A market record from gamma-api.polymarket.com/markets."""

    gamma_id: str
    condition_id: str
    slug: str
    question: str
    category: str
    end_date: str  # ISO-8601
    outcomes: list[str]
    outcome_prices: list[float]
    volume_usdc: float
    liquidity_usdc: float
    closed: bool
    active: bool
    last_trade_price: float | None
    best_bid: float | None
    best_ask: float | None
    fetched_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketWalletValue:
    """A wallet's current total portfolio value (USD)."""

    proxy_wallet: str
    value_usdc: float
    snapshot_at: str = field(default_factory=_utcnow_iso)
