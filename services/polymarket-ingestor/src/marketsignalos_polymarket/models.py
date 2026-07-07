"""
Polymarket data models.

Field names are derived from the actual API responses captured in Phase 0
(see docs/polymarket-api-discovery.md). All wallet addresses are normalized
to lowercase hex; conditionIds keep their 0x-prefixed canonical form.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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
    current_outcome_price: float = 0.0
    cash_pnl_usdc: float = 0.0
    snapshot_id: str = ""
    snapshot_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketPositionSnapshot:
    """Manifest for one complete paginated /positions fetch."""

    proxy_wallet: str
    snapshot_id: str
    position_count: int
    complete: bool
    snapshot_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketClosedPosition:
    """An audited row from data-api.polymarket.com/closed-positions."""

    proxy_wallet: str
    condition_id: str
    outcome_index: int
    outcome: str
    size: float
    avg_price: float
    realized_pnl_usdc: float
    current_outcome_price: float
    slug: str
    title: str
    event_slug: str
    fetched_at: str = field(default_factory=_utcnow_iso)


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
class PolymarketPriceSnapshot:
    """
    One observed price point for one market — appended every time the
    pipeline fetches the market from Gamma. This is the price HISTORY the
    deduplicating markets store deliberately discards, retained for
    closing-line-value: the last snapshot where the market was still open is
    the "closing line" a resolved bet is scored against.
    """

    condition_id: str
    yes_price: float  # last trade, else bid/ask mid; 0.0 when unknown
    active: bool
    closed: bool
    fetched_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class PolymarketWalletBet:
    """
    One (wallet, condition, outcome) betting record — the per-position detail
    behind the enrichment aggregates, rewritten each enrichment pass. Powers
    the wallet detail page (bet history, equity curve, category breakdown).

    status:
      - "won" / "lost":  held through resolution
      - "exited":        fully sold before resolution
      - "open":          still held on an unresolved market
    """

    proxy_wallet: str
    condition_id: str
    outcome_index: int
    outcome: str
    event_slug: str
    category: str
    title: str
    status: str
    entry_price: float          # buys-only VWAP (sells don't distort it)
    cost_usdc: float            # total USDC put into the position (buys)
    net_size: float             # shares still held at the latest activity
    realized_pnl_usdc: float    # locked in by sells before resolution
    total_pnl_usdc: float       # realized + resolution payoff where settled
    clv: float | None           # closing/current line minus entry; None if no price history
    last_trade_ts: int
    computed_at: str = field(default_factory=_utcnow_iso)


@dataclass(frozen=True, slots=True)
class MarketLink:
    """
    A candidate Polymarket → Kalshi market identifier mapping.

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
    # Discovery provenance: which sources surfaced this wallet, e.g.
    # "leaderboard" (categorized matrix sweep) and/or "subgraph" (recent
    # on-chain fills). Empty on rows written before this field existed.
    sources: list[str] = field(default_factory=list)
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
    forecast_skill_likelihood: float = 0.0
    forecast_edge_mean: float = 0.0
    forecast_edge_lower_bound: float = 0.0
    independent_settled_events: float = 0.0
    all_time_pnl_usdc: float = 0.0
    all_time_volume_usdc: float = 0.0
    all_time_roi: float = 0.0
    pnl_30d_usdc: float = 0.0
    active_pnl_usdc: float = 0.0
    max_drawdown_usdc: float = 0.0
    # Recency-weighted ("recent form") edge fit: same model, with each bet's
    # likelihood weight decayed by RECENCY_HALF_LIFE_DAYS half-lives between
    # its last fill and the newest bet in the enrichment pass.
    recent_skill_likelihood: float = 0.0
    recent_edge_mean: float = 0.0
    recent_edge_lower_bound: float = 0.0
    recent_independent_events: float = 0.0
    # Closing-line value: average (closing/current price − entry price) of the
    # picked outcome, event-capped and capital-weighted. Positive CLV means
    # the wallet consistently buys before the market moves its way — measured
    # without needing resolution, so it also scores open and exited-early
    # positions the resolution fit can't see.
    clv_mean: float = 0.0
    clv_lower_bound: float = 0.0   # mean − 1.645·SE (5th-percentile, normal approx)
    clv_sample_size: float = 0.0   # event-capped effective observations
    # Per-category edge decomposition: the same Bayesian fit re-run on each
    # Gamma category's bets with the wallet's own lifetime posterior as the
    # prior (partial pooling — thin categories shrink back to the wallet-level
    # estimate). One dict per category the wallet has settled bets in:
    #   {category, skill_likelihood, edge_mean, edge_lower_bound,
    #    independent_events}
    # sorted by independent_events descending. Consumers should require
    # >= CATEGORY_MIN_INDEPENDENT_EVENTS before trusting a category fit over
    # the wallet-level score.
    category_edges: list[dict[str, Any]] = field(default_factory=list)
    # Price-lead footprint (see price_lead.py): does the market tend to
    # follow this wallet's entries? Descriptive only — never gates
    # tailability. Score is the mean of the available sub-scores; the raw
    # sub-signal values ride along for the wallet dossier.
    price_lead_score: float = 0.0
    price_lead_drivers: list[str] = field(default_factory=list)
    post_entry_drift_48h: float = 0.0      # mean picked-side move, $/share
    post_entry_drift_samples: float = 0.0  # weighted bets with a 48h observation
    longshot_win_rate: float = 0.0
    longshot_implied_rate: float = 0.0
    longshot_events: float = 0.0
    late_entry_win_rate: float = 0.0
    late_entry_implied_rate: float = 0.0
    late_entry_events: float = 0.0
    # Trader-style classification (see trader_style.py): deterministic,
    # non-accusatory descriptors of HOW the wallet trades. Never gates
    # tailability — it labels operational footprint, not edge.
    style_archetype: str = "unclassified"  # systematic | mixed | discretionary | unclassified
    automation_score: float = 0.0          # 0..1 mean of available style sub-scores
    style_drivers: list[str] = field(default_factory=list)
    trades_per_active_day: float = 0.0
    median_intertrade_gap_seconds: float = 0.0  # 0 = too few trades to measure
    active_utc_hours: int = 0                   # distinct UTC hours-of-day traded
    buy_size_uniformity: float = 0.0            # share of buys at a top-3 modal size
    markets_per_active_day: float = 0.0
    top_category: str = ""
    top_category_share: float = 0.0
    exited_position_share: float = 0.0
    median_exit_hold_hours: float = 0.0
    data_quality_status: str = "untrusted"
    data_quality_reasons: list[str] = field(default_factory=list)
    economic_qualified: bool = False
    tailability_status: str = "blocked"
    tailability_reasons: list[str] = field(default_factory=list)
    score_version: str = "forecast-v4"
    computed_at: str = field(default_factory=_utcnow_iso)


@dataclass(slots=True)
class PolymarketWalletHydration:
    """Trust inputs accumulated while a wallet is hydrated."""

    proxy_wallet: str
    newest_activity_timestamp: int | None = None
    oldest_activity_cursor_timestamp: int | None = None
    activity_history_complete: bool = False
    positions_complete: bool = False
    closed_positions_complete: bool = False
    economic_all_time_complete: bool = False
    economic_month_complete: bool = False
    metadata_condition_count: int = 0
    metadata_covered_count: int = 0
    metadata_coverage: float = 0.0
    all_time_pnl_usdc: float = 0.0
    all_time_volume_usdc: float = 0.0
    pnl_30d_usdc: float = 0.0
    active_pnl_usdc: float = 0.0
    max_drawdown_usdc: float = 0.0
    latest_position_snapshot_id: str | None = None
    last_refreshed_at: str | None = None
    errors: list[str] = field(default_factory=list)
