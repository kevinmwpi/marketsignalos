from __future__ import annotations

from fastapi import APIRouter, Query

from marketsignalos_api.services.orderflow_analytics import (
    OrderflowAnomaly,
    detect_orderflow_anomalies,
)


router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/orderflow", response_model=list[OrderflowAnomaly])
def orderflow_anomalies(
    limit: int = Query(default=50, ge=1, le=500),
    max_rows: int = Query(default=5000, ge=100, le=50000),
    min_odds_jump: float = Query(default=10.0, ge=1.0, le=99.0),
    min_size_zscore: float = Query(default=2.5, ge=1.0, le=10.0),
    min_large_quantity: int = Query(default=100, ge=1, le=100000),
) -> list[OrderflowAnomaly]:
    return detect_orderflow_anomalies(
        max_rows=max_rows,
        limit=limit,
        min_odds_jump=min_odds_jump,
        min_size_zscore=min_size_zscore,
        min_large_quantity=min_large_quantity,
    )
