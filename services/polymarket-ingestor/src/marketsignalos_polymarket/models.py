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


@dataclass(frozen=True, slots=True)
class MarketLink:
    """
    A candidate cross-exchange identifier mapping.

    status:
      - "approved":  high-confidence match, used by downstream signals
      - "pending":   mid-confidence, awaiting manual review
      - "rejected":  explicitly disqualified (used to silence repeat false positives)
    matched_by:
      - "auto":   produced by the similarity matcher
      - "manual": approved/rejected by a human via review-matches CLI
    """

    kalshi_ticker: str
    polymarket_condition_id: str
    polymarket_slug: str
    kalshi_title: str
    polymarket_title: str
    kalshi_end_date: str
    polymarket_end_date: str
    confidence: float
    status: str
    matched_by: str
    matched_at: str = field(default_factory=_utcnow_iso)


@dataclass(slots=True)
class PolymarketWalletReviewState:
    """Per-wallet bookkeeping for the deep-review hydration pass.

    Mutable on purpose: a single deep-leaderboard sweep visits the same wallet
    across many slices and accumulates appearances/categories/time_periods/
    orders before the final atomic rewrite. The rest of the codebase uses
    frozen records because those rows are immutable events; review-state is
    explicitly stateful.

    status:
      - "active":   in the polling rotation
      - "archived": dormant and absent from the latest deep sweep; skipped
                    during hydration but kept in JSONL for re-activation
      - "pinned":   never archived, regardless of activity or discovery
    """

    proxy_wallet: str
    status: str
    first_seen_at: str
    last_leaderboard_seen_at: str | None = None
    last_activity_at: int | None = None  # unix seconds, mirrors enrichment
    last_polled_at: str | None = None    # ISO timestamp of last hydration
    best_rank: int | None = None         # lowest (= best) offset+row across slices
    appearances: int = 0
    categories: list[str] = field(default_factory=list)
    time_periods: list[str] = field(default_factory=list)
    orders: list[str] = field(default_factory=list)
    archived_at: str | None = None
    archived_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PolymarketWalletEnrichment:
    """
    On-chain skill metrics for a wallet, derived from joining its activity
    stream against resolved markets. Recomputed each enrichment pass.

    The score is a hierarchical Bayesian logistic edge model:
        y_i ~ Bernoulli(q_i),    logit(q_i) = logit(p_i) + edge_w
        edge_w ~ Normal(mu_pop, sigma2_pop)        (empirical Bayes)

    so `edge_*` fields are in log-odds units relative to the market's
    own implied price; `skill_likelihood` is P(edge_w > 0 | data).
    See bayesian_skill.py for the derivation.
    """

    proxy_wallet: str
    name: str
    pseudonym: str
    resolved_trades: int  # bets that settled (won + lost)
    wins: int
    losses: int
    win_rate: float
    skill_likelihood: float       # P(edge > 0 | data) — kept name for API back-compat
    stddevs_above_expected: float # edge_mean / sqrt(edge_var) — posterior z-score
    edge_mean: float              # posterior MAP of edge_w (log-odds)
    edge_lower_bound: float       # 5th-percentile lower bound of edge_w
    effective_sample_size: float  # event-correlation-adjusted resolved bets
    resolved_volume_usdc: float   # USD volume in resolved bets only (rank weight)
    rank_score: float             # posterior_skill * max(0, edge_lower_bound) * log1p(resolved_volume_usdc)
    total_volume_usdc: float      # sum of |usdc_size| across all TRADE events
    total_pnl_usdc: float         # realized + unrealized-at-resolution
    avg_position_size_usdc: float
    trade_count: int              # total TRADE events (not bets — fills can be multiple per bet)
    last_activity_at: int         # unix timestamp of most recent activity
    computed_at: str = field(default_factory=_utcnow_iso)
