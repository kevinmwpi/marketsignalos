from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from marketsignalos_api._paths import polymarket_enrichment_path
from marketsignalos_api.services.external_urls import polymarket_profile_url

router = APIRouter(prefix="/signals", tags=["signals"])


class PolymarketWalletSkill(BaseModel):
    proxy_wallet: str
    name: str
    pseudonym: str
    resolved_trades: int
    wins: int
    losses: int
    win_rate: float
    skill_likelihood: float        # P(edge > 0 | data)
    stddevs_above_expected: float  # edge_mean / sqrt(edge_var)
    edge_mean: float = 0.0
    edge_lower_bound: float = 0.0
    effective_sample_size: float = 0.0
    resolved_volume_usdc: float = 0.0
    rank_score: float = 0.0
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
    recent_skill_likelihood: float = 0.0
    recent_edge_mean: float = 0.0
    recent_edge_lower_bound: float = 0.0
    recent_independent_events: float = 0.0
    clv_mean: float = 0.0
    clv_lower_bound: float = 0.0
    clv_sample_size: float = 0.0
    style_archetype: str = "unclassified"
    automation_score: float = 0.0
    style_drivers: list[str] = Field(default_factory=list)
    trades_per_active_day: float = 0.0
    median_intertrade_gap_seconds: float = 0.0
    active_utc_hours: int = 0
    buy_size_uniformity: float = 0.0
    markets_per_active_day: float = 0.0
    top_category: str = ""
    top_category_share: float = 0.0
    exited_position_share: float = 0.0
    median_exit_hold_hours: float = 0.0
    data_quality_status: str = "untrusted"
    data_quality_reasons: list[str] = Field(default_factory=list)
    economic_qualified: bool = False
    tailability_status: str = "blocked"
    tailability_reasons: list[str] = Field(default_factory=list)
    score_version: str = "legacy"
    total_volume_usdc: float
    total_pnl_usdc: float
    avg_position_size_usdc: float
    trade_count: int
    last_activity_at: int
    computed_at: str
    polymarket_profile_url: str


def _read_enrichment_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


@router.get("/polymarket-leaderboard", response_model=list[PolymarketWalletSkill])
def polymarket_leaderboard(
    min_resolved: int = Query(default=20, ge=1, le=10_000),
    limit: int = Query(default=50, ge=1, le=500),
    min_skill: float = Query(default=0.0, ge=0.0, le=1.0),
    tailability: Literal["all", "tailable", "blocked"] = Query(default="all"),
    archetype: Literal[
        "all", "systematic", "mixed", "discretionary", "unclassified"
    ] = Query(
        default="all",
        description=(
            "Filter by trading-style archetype, e.g. archetype=systematic for "
            "automation-shaped wallets."
        ),
    ),
) -> list[PolymarketWalletSkill]:
    """
    Skill-ranked Polymarket wallets, computed from on-chain activity vs.
    resolved-market outcomes. Run the polymarket ingestor with mode=enrichment
    (or mode=all) to refresh the underlying data.
    """
    rows = _read_enrichment_rows(polymarket_enrichment_path())
    eligible: list[PolymarketWalletSkill] = []
    for row in rows:
        resolved = int(row.get("resolved_trades", 0) or 0)
        if resolved < min_resolved:
            continue
        skill = float(row.get("skill_likelihood", 0) or 0)
        if skill < min_skill:
            continue
        status = str(row.get("tailability_status", "blocked"))
        if tailability != "all" and status != tailability:
            continue
        style = str(row.get("style_archetype", "") or "unclassified")
        if archetype != "all" and style != archetype:
            continue
        try:
            eligible.append(
                PolymarketWalletSkill(
                    **row,
                    polymarket_profile_url=polymarket_profile_url(
                        str(row.get("proxy_wallet", ""))
                    ),
                )
            )
        except (TypeError, ValueError):
            continue
    # rank_score is the new conservative ranking — falls back to
    # skill_likelihood ordering when older enrichment rows have rank_score=0.
    eligible.sort(
        key=lambda r: (
            0 if r.tailability_status == "tailable" else 1,
            -r.rank_score,
            -r.skill_likelihood,
            -r.resolved_trades,
            r.proxy_wallet,
        )
    )
    return eligible[:limit]
