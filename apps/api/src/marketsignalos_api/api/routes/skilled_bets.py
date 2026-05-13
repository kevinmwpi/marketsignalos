from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel

from marketsignalos_api.services.skilled_bets import compute_skilled_bets


router = APIRouter(prefix="/signals", tags=["signals"])


class SkilledBetOut(BaseModel):
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float
    resolved_trades: int
    win_rate: float

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

    polymarket_profile_url: str
    polymarket_market_url: str


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
