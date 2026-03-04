from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from marketsignalos_api.services.account_enrichment_ingestion import collect_account_enrichment
from marketsignalos_api.services.account_surface_ingestion import collect_surface_account_stats
from marketsignalos_api.services.skill_scoring import rank_accounts_by_skill


router = APIRouter(prefix="/signals", tags=["signals"])


class SkilledAccountSignal(BaseModel):
    account_id: str
    account_first_seen_at: str
    account_age_days: int
    resolved_calls: int
    wins: int
    losses: int
    win_rate: float
    expected_wins: float
    excess_wins: float
    stddevs_above_expected: float
    skill_likelihood: float
    insider_like_score: float
    anomaly_probability: float
    last_activity_at: str


@router.get("/leaderboard", response_model=list[SkilledAccountSignal])
def leaderboard(
    fresh_days: int = Query(default=30, ge=1, le=365),
    min_resolved: int = Query(default=20, ge=1, le=5000),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[SkilledAccountSignal]:
    surface = collect_surface_account_stats(fresh_days=fresh_days, min_resolved=min_resolved)
    account_ids = {row.account_id for row in surface}
    enrichment = collect_account_enrichment(account_ids)

    ranked = rank_accounts_by_skill(surface=surface, enrichment=enrichment, limit=limit)
    return [SkilledAccountSignal.model_validate(row, from_attributes=True) for row in ranked]

