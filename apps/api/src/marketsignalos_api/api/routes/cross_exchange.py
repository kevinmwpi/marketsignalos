from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel

from marketsignalos_api.services.cross_exchange import compute_cross_exchange_signals


router = APIRouter(prefix="/signals", tags=["signals"])


class CrossExchangeSignalOut(BaseModel):
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float
    resolved_trades: int

    polymarket_condition_id: str
    polymarket_slug: str
    polymarket_title: str
    polymarket_outcome_index: int
    polymarket_outcome: str
    polymarket_yes_price: float
    polymarket_position_size: float
    polymarket_position_value_usdc: float

    kalshi_ticker: str
    kalshi_title: str
    kalshi_yes_price: float

    price_dislocation: float
    price_dislocation_pct: float
    cheaper_exchange: str
    recommended_action: str

    match_confidence: float
    match_status: str

    opportunity_score: float
    observed_at: str


@router.get("/cross-exchange", response_model=list[CrossExchangeSignalOut])
def cross_exchange(
    min_skill: float = Query(default=0.6, ge=0.0, le=1.0),
    min_resolved: int = Query(default=5, ge=1, le=10_000),
    min_dislocation_pct: float = Query(default=2.0, ge=0.0, le=100.0),
    min_position_value_usdc: float = Query(default=0.0, ge=0.0),
    only_approved_links: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=500),
) -> list[CrossExchangeSignalOut]:
    """
    Tradeable price dislocations between Kalshi and Polymarket.

    For each currently-open Polymarket position held by a wallet with
    skill_likelihood >= min_skill (and at least min_resolved bets settled),
    finds the linked Kalshi market via market_links.jsonl and computes the
    YES-price difference. Returns the top `limit` ranked by
    opportunity_score = dislocation_pct * skill_likelihood * log1p(position_value).
    """
    signals = compute_cross_exchange_signals(
        min_skill=min_skill,
        min_resolved=min_resolved,
        min_dislocation_pct=min_dislocation_pct,
        min_position_value_usdc=min_position_value_usdc,
        only_approved_links=only_approved_links,
    )
    return [CrossExchangeSignalOut(**asdict(s)) for s in signals[:limit]]
