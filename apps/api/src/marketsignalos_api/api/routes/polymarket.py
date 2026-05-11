from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from marketsignalos_api._paths import polymarket_enrichment_path


router = APIRouter(prefix="/signals", tags=["signals"])


class PolymarketWalletSkill(BaseModel):
    proxy_wallet: str
    name: str
    pseudonym: str
    resolved_trades: int
    wins: int
    losses: int
    win_rate: float
    skill_likelihood: float
    stddevs_above_expected: float
    total_volume_usdc: float
    total_pnl_usdc: float
    avg_position_size_usdc: float
    trade_count: int
    last_activity_at: int
    computed_at: str


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
        try:
            eligible.append(PolymarketWalletSkill(**row))
        except (TypeError, ValueError):
            continue
    eligible.sort(
        key=lambda r: (-r.skill_likelihood, -r.resolved_trades, r.proxy_wallet)
    )
    return eligible[:limit]
