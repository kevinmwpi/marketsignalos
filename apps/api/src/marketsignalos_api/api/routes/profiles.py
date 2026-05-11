from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from marketsignalos_api._paths import profile_snapshots_path

router = APIRouter(prefix="/signals", tags=["signals"])


class ProfileHistoryPoint(BaseModel):
    scraped_at: str
    source: str
    rank: int | None
    profit_dollars: float | None
    roi_pct: float | None
    win_rate: float | None
    resolved_trades: int | None
    open_trades: int | None
    categories: list[str]


def _binomial_skill_likelihood(win_rate: float | None, resolved_trades: int | None) -> float | None:
    """
    Compute skill_likelihood via normal approximation of a binomial test
    against p=0.5 (null: 50% win rate by chance).
    Returns None when data is insufficient (< 10 resolved trades).
    """
    if win_rate is None or resolved_trades is None or resolved_trades < 10:
        return None
    wins = win_rate * resolved_trades
    expected = resolved_trades * 0.5
    std = math.sqrt(resolved_trades * 0.5 * 0.5)
    z = (wins - expected) / std
    return round(min(max(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))), 0.0), 1.0), 6)


class ProfileSummary(BaseModel):
    username: str
    snapshot_count: int
    first_seen_at: str
    last_seen_at: str
    latest_rank: int | None
    latest_win_rate: float | None
    latest_profit_dollars: float | None
    latest_resolved_trades: int | None
    skill_likelihood: float | None
    categories: list[str]


def _load_snapshots(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned:
            try:
                rows.append(json.loads(cleaned))
            except json.JSONDecodeError:
                pass
    return rows


def _build_summaries(rows: list[dict[str, object]]) -> list[ProfileSummary]:
    by_user: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        username = str(row.get("username") or "")
        if not username:
            continue
        by_user.setdefault(username, []).append(row)

    summaries: list[ProfileSummary] = []
    for username, snaps in by_user.items():
        snaps_sorted = sorted(snaps, key=lambda r: str(r.get("scraped_at") or ""))
        latest = snaps_sorted[-1]
        timestamps = [str(s.get("scraped_at") or "") for s in snaps_sorted if s.get("scraped_at")]
        all_cats: list[str] = []
        for s in snaps_sorted:
            cats = s.get("categories")
            if isinstance(cats, list):
                all_cats.extend(str(c) for c in cats)
        unique_cats = list(dict.fromkeys(all_cats))

        latest_win_rate: float | None = latest.get("win_rate")  # type: ignore[assignment]
        latest_resolved_trades: int | None = latest.get("resolved_trades")  # type: ignore[assignment]
        summaries.append(
            ProfileSummary(
                username=username,
                snapshot_count=len(snaps_sorted),
                first_seen_at=timestamps[0] if timestamps else "",
                last_seen_at=timestamps[-1] if timestamps else "",
                latest_rank=latest.get("rank"),  # type: ignore[arg-type]
                latest_win_rate=latest_win_rate,
                latest_profit_dollars=latest.get("profit_dollars"),  # type: ignore[arg-type]
                latest_resolved_trades=latest_resolved_trades,
                skill_likelihood=_binomial_skill_likelihood(latest_win_rate, latest_resolved_trades),
                categories=unique_cats,
            )
        )

    summaries.sort(key=lambda s: (s.latest_rank is None, s.latest_rank or 9999))
    return summaries


@router.get("/profiles", response_model=list[ProfileSummary])
def list_profiles(
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[ProfileSummary]:
    rows = _load_snapshots(profile_snapshots_path())
    summaries = _build_summaries(rows)
    return summaries[:limit]


@router.get("/profiles/{username}/history", response_model=list[ProfileHistoryPoint])
def get_profile_history(username: str) -> list[ProfileHistoryPoint]:
    rows = _load_snapshots(profile_snapshots_path())
    user_rows = [r for r in rows if str(r.get("username") or "").lower() == username.lower()]
    if not user_rows:
        raise HTTPException(status_code=404, detail=f"No snapshots found for user '{username}'")

    user_rows.sort(key=lambda r: str(r.get("scraped_at") or ""))
    points: list[ProfileHistoryPoint] = []
    for row in user_rows:
        cats = row.get("categories")
        points.append(
            ProfileHistoryPoint(
                scraped_at=str(row.get("scraped_at") or ""),
                source=str(row.get("source") or "unknown"),
                rank=row.get("rank"),  # type: ignore[arg-type]
                profit_dollars=row.get("profit_dollars"),  # type: ignore[arg-type]
                roi_pct=row.get("roi_pct"),  # type: ignore[arg-type]
                win_rate=row.get("win_rate"),  # type: ignore[arg-type]
                resolved_trades=row.get("resolved_trades"),  # type: ignore[arg-type]
                open_trades=row.get("open_trades"),  # type: ignore[arg-type]
                categories=list(cats) if isinstance(cats, list) else [],
            )
        )
    return points
