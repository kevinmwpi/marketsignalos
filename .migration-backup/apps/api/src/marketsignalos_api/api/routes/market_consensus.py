from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from marketsignalos_api.services.market_consensus import compute_market_consensus

router = APIRouter(prefix="/signals", tags=["signals"])


class ConsensusSideOut(BaseModel):
    outcome_index: int
    outcome: str
    wallets: int   # distinct entities (name-clustered)
    accounts: int  # raw wallet addresses before clustering
    wallet_names: list[str] = Field(default_factory=list)
    total_position_value_usdc: float
    avg_entry_price: float
    avg_skill_likelihood: float
    latest_entry_at: int


class MarketConsensusOut(BaseModel):
    condition_id: str
    title: str
    category: str
    event_slug: str
    polymarket_market_url: str
    kalshi_market_url: str
    tradability: str
    net_direction: str
    total_wallets: int
    sides: list[ConsensusSideOut] = Field(default_factory=list)


@router.get("/market-consensus", response_model=list[MarketConsensusOut])
def market_consensus(
    min_skill: float = Query(default=0.8, ge=0.0, le=1.0),
    min_resolved: int = Query(default=20, ge=1, le=10_000),
    min_independent_events: float = Query(default=20.0, ge=0.0),
    require_positive_edge: bool = Query(default=False),
    min_wallets: int = Query(
        default=2,
        ge=1,
        le=100,
        description="Only markets where at least this many skilled wallets hold a side.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[MarketConsensusOut]:
    """
    Markets grouped by skilled-wallet confluence: how many distinct skilled
    wallets hold each side, with capital-weighted average entries.
    `net_direction` is "contested" when skilled wallets hold BOTH sides.
    Sorted by wallet count, then total capital at risk.
    """
    rows = compute_market_consensus(
        min_skill=min_skill,
        min_resolved=min_resolved,
        min_independent_events=min_independent_events,
        require_positive_edge=require_positive_edge,
        min_wallets=min_wallets,
        limit=limit,
    )
    return [
        MarketConsensusOut(
            condition_id=row.condition_id,
            title=row.title,
            category=row.category,
            event_slug=row.event_slug,
            polymarket_market_url=row.polymarket_market_url,
            kalshi_market_url=row.kalshi_market_url,
            tradability=row.tradability,
            net_direction=row.net_direction,
            total_wallets=row.total_wallets,
            sides=[
                ConsensusSideOut(
                    outcome_index=side.outcome_index,
                    outcome=side.outcome,
                    wallets=side.wallets,
                    accounts=side.accounts,
                    wallet_names=side.wallet_names,
                    total_position_value_usdc=side.total_position_value_usdc,
                    avg_entry_price=side.avg_entry_price,
                    avg_skill_likelihood=side.avg_skill_likelihood,
                    latest_entry_at=side.latest_entry_at,
                )
                for side in row.sides
            ],
        )
        for row in rows
    ]
