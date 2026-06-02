from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel

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


@router.get("/skilled-bets", response_model=list[SkilledBetOut])
def skilled_bets(
    min_skill: float = Query(default=0.8, ge=0.0, le=1.0),
    min_resolved: int = Query(default=20, ge=1, le=10_000),
    min_position_value_usdc: float = Query(default=0.0, ge=0.0),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[SkilledBetOut]:
    """
    Recent BUY entries from skilled Polymarket wallets that are STILL HELD,
    sorted by entry timestamp (newest first). "Skilled" = enrichment row
    with skill_likelihood >= min_skill and resolved_trades >= min_resolved.

    Use this feed to tail/monitor: each row shows the wallet, the market,
    the buy price + size, the still-open position, and deep links into
    Polymarket for the wallet and the event.
    """
    signals = compute_skilled_bets(
        min_skill=min_skill,
        min_resolved=min_resolved,
        min_position_value_usdc=min_position_value_usdc,
    )
    return [SkilledBetOut(**asdict(s)) for s in signals[:limit]]


@router.get("/skilled-bets/summary")
def skilled_bets_summary() -> dict[str, object]:
    """Trust-state summary used by the dashboard rebuild banner."""
    return summarize_skilled_bets()
