from __future__ import annotations

from fastapi import APIRouter, Query

from marketsignalos_api.services.opportunity_scoring import (
    OpportunityDashboard,
    build_opportunity_dashboard,
)


router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/opportunities", response_model=OpportunityDashboard)
def opportunities(
    fresh_days: int = Query(default=30, ge=1, le=365),
    min_resolved: int = Query(default=20, ge=1, le=5000),
    limit: int = Query(default=12, ge=1, le=100),
    max_rows: int = Query(default=5000, ge=100, le=50000),
    min_odds_jump: float = Query(default=10.0, ge=1.0, le=99.0),
    min_size_zscore: float = Query(default=2.5, ge=1.0, le=10.0),
    min_large_quantity: int = Query(default=100, ge=1, le=100000),
) -> OpportunityDashboard:
    return build_opportunity_dashboard(
        fresh_days=fresh_days,
        min_resolved=min_resolved,
        limit=limit,
        max_rows=max_rows,
        min_odds_jump=min_odds_jump,
        min_size_zscore=min_size_zscore,
        min_large_quantity=min_large_quantity,
    )
