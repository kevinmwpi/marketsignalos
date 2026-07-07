from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from marketsignalos_api.services.skilled_bets import compute_skilled_bets, summarize_skilled_bets


router = APIRouter(prefix="/signals", tags=["signals"])


class SkilledBetOut(BaseModel):
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float
    resolved_trades: int
    win_rate: float
    edge_mean: float
    edge_lower_bound: float
    forecast_skill_likelihood: float
    forecast_edge_mean: float
    forecast_edge_lower_bound: float
    independent_settled_events: float
    all_time_pnl_usdc: float
    all_time_volume_usdc: float
    all_time_roi: float
    pnl_30d_usdc: float
    active_pnl_usdc: float
    max_drawdown_usdc: float
    data_quality_status: str
    data_quality_reasons: list[str]
    economic_qualified: bool
    tailability_status: str
    tailability_reasons: list[str]
    score_version: str

    condition_id: str
    slug: str
    event_slug: str
    title: str
    category: str

    outcome_index: int
    outcome: str
    entry_price: float
    entry_size: float
    entry_usdc_size: float
    transaction_hash: str
    bought_at: int
    signal_age_days: int

    current_position_size: float
    current_position_value_usdc: float
    current_market_yes_price: float
    current_outcome_price: float

    polymarket_profile_url: str
    polymarket_market_url: str

    kalshi_ticker: str
    kalshi_event_ticker: str
    kalshi_title: str
    kalshi_market_url: str
    kalshi_yes_price: float
    kalshi_match_confidence: float
    kalshi_match_status: str
    kalshi_vs_entry_cents: float

    tradability: str
    tradability_reasons: list[str] = Field(default_factory=list)

    move_captured_pct: float
    remaining_edge_status: str

    consensus_wallets: int
    consensus_contested: bool

    conviction: str
    conviction_z: float
    entry_pct_of_bankroll: float

    clv_mean: float
    clv_lower_bound: float
    clv_sample_size: float

    wallet_archetype: str
    wallet_automation_score: float
    wallet_price_lead_score: float

    tail_edge_used: float
    tail_fair_price: float
    tail_ev: float
    tail_ev_status: str

    category_skill_likelihood: float
    category_edge_lower_bound: float
    category_independent_events: float
    category_skill_source: str


@router.get("/skilled-bets", response_model=list[SkilledBetOut])
def skilled_bets(
    min_skill: float = Query(default=0.8, ge=0.0, le=1.0),
    min_resolved: int = Query(default=20, ge=1, le=10_000),
    min_independent_events: float = Query(default=20.0, ge=0.0),
    min_position_value_usdc: float = Query(default=0.0, ge=0.0),
    max_bet_age_days: int = Query(
        default=0,
        ge=0,
        le=3650,
        description="When >0, only include bets whose latest BUY is at most this many days old.",
    ),
    require_positive_edge: bool = Query(
        default=False,
        description="When true, require wallet edge_lower_bound > 0.",
    ),
    include_untradable: bool = Query(
        default=False,
        description="When true, include closed/on-chain-only bets with no execution path.",
    ),
    max_move_captured: float = Query(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "When <1, drop signals whose picked outcome has already captured at "
            "least this fraction of the entry→$1 move (the thesis is priced in)."
        ),
    ),
    min_tail_ev: float | None = Query(
        default=None,
        ge=-1.0,
        le=1.0,
        description=(
            "When set, only include signals whose tail EV at today's price is "
            "at least this many dollars per share; signals without a live "
            "price (tail_ev_status=unknown) are dropped."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[SkilledBetOut]:
    """
    Recent BUY entries from skilled Polymarket wallets that are STILL HELD.
    Fresh/discounted entries (price still near or below the wallet's entry)
    rank above partially-priced ones; fully-converged "late" signals sort
    last. Within each remaining-edge rank: high-conviction entries first,
    then best tail EV at today's price, then newest BUY. By default only
    actionable bets (`tradability` of poly_direct or kalshi_mirror) are
    returned.
    """
    signals = compute_skilled_bets(
        min_skill=min_skill,
        min_resolved=min_resolved,
        min_independent_events=min_independent_events,
        min_position_value_usdc=min_position_value_usdc,
        max_bet_age_days=max_bet_age_days,
        require_positive_edge=require_positive_edge,
        include_untradable=include_untradable,
        max_move_captured=max_move_captured,
        limit=None if min_tail_ev is not None else limit,
    )
    if min_tail_ev is not None:
        signals = [
            s
            for s in signals
            if s.tail_ev_status != "unknown" and s.tail_ev >= min_tail_ev
        ][:limit]
    return [SkilledBetOut(**asdict(s)) for s in signals]


@router.get("/skilled-bets/summary")
def skilled_bets_summary() -> dict[str, object]:
    """Trust-state and feed tradability summary for the dashboard."""
    return summarize_skilled_bets()
