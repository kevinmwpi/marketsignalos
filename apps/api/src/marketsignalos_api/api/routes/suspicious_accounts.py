from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel


router = APIRouter(prefix="/signals", tags=["signals"])


class SuspiciousAccountSignal(BaseModel):
    account_id: str
    account_first_seen_at: str
    account_age_days: int
    resolved_calls: int
    wins: int
    losses: int
    win_rate: float
    suspicion_score: float
    stddevs_above_expected: float
    statistically_significant: bool
    last_activity_at: str


class _Fill(BaseModel):
    source: str
    account_id: str
    market_ticker: str
    fill_id: str
    trade_id: str
    side: str
    price: float
    quantity: int
    traded_at: str


class _Resolution(BaseModel):
    source: str
    market_ticker: str
    resolved_at: str
    settlement_side: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[6]


def _fills_store_path() -> Path:
    configured = os.getenv("INGESTOR_FILLS_STORE_PATH")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_fills.jsonl"


def _resolution_store_path() -> Path:
    configured = os.getenv("INGESTOR_RESOLUTIONS_STORE_PATH")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_market_resolutions.jsonl"


def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _build_suspicious_signals(
    *,
    fresh_days: int,
    min_resolved: int,
    limit: int,
    baseline_win_rate: float,
    sigma_threshold: float,
    only_significant: bool,
    now: datetime | None = None,
) -> list[SuspiciousAccountSignal]:
    fills = [_Fill.model_validate(row) for row in _load_jsonl(_fills_store_path())]
    resolutions = [_Resolution.model_validate(row) for row in _load_jsonl(_resolution_store_path())]
    if not fills or not resolutions:
        return []

    now_dt = now or datetime.now(tz=UTC)
    settlement_by_market = {row.market_ticker: row for row in resolutions}

    wins_by_account: dict[str, int] = defaultdict(int)
    losses_by_account: dict[str, int] = defaultdict(int)
    first_seen_by_account: dict[str, datetime] = {}
    last_seen_by_account: dict[str, datetime] = {}

    for fill in fills:
        resolution = settlement_by_market.get(fill.market_ticker)
        if resolution is None:
            continue

        traded_at = _parse_utc(fill.traded_at)
        if fill.account_id not in first_seen_by_account or traded_at < first_seen_by_account[fill.account_id]:
            first_seen_by_account[fill.account_id] = traded_at
        if fill.account_id not in last_seen_by_account or traded_at > last_seen_by_account[fill.account_id]:
            last_seen_by_account[fill.account_id] = traded_at

        if fill.side == resolution.settlement_side:
            wins_by_account[fill.account_id] += 1
        else:
            losses_by_account[fill.account_id] += 1

    signals: list[SuspiciousAccountSignal] = []
    for account_id, first_seen in first_seen_by_account.items():
        wins = wins_by_account[account_id]
        losses = losses_by_account[account_id]
        resolved_calls = wins + losses
        if resolved_calls < min_resolved:
            continue

        age_days = max(0, (now_dt - first_seen).days)
        if age_days > fresh_days:
            continue

        win_rate = wins / resolved_calls
        expected_wins = resolved_calls * baseline_win_rate
        stddev = math.sqrt(resolved_calls * baseline_win_rate * (1.0 - baseline_win_rate))
        stddevs_above_expected = (wins - expected_wins) / stddev if stddev > 0 else 0.0
        statistically_significant = stddevs_above_expected >= sigma_threshold
        if only_significant and not statistically_significant:
            continue
        suspicion_score = stddevs_above_expected

        signals.append(
            SuspiciousAccountSignal(
                account_id=account_id,
                account_first_seen_at=first_seen.isoformat().replace("+00:00", "Z"),
                account_age_days=age_days,
                resolved_calls=resolved_calls,
                wins=wins,
                losses=losses,
                win_rate=win_rate,
                suspicion_score=suspicion_score,
                stddevs_above_expected=stddevs_above_expected,
                statistically_significant=statistically_significant,
                last_activity_at=last_seen_by_account[account_id].isoformat().replace("+00:00", "Z"),
            )
        )

    signals.sort(key=lambda row: (row.suspicion_score, row.resolved_calls), reverse=True)
    return signals[:limit]


@router.get("/suspicious-accounts", response_model=list[SuspiciousAccountSignal])
def list_suspicious_accounts(
    fresh_days: int = Query(default=30, ge=1, le=365),
    min_resolved: int = Query(default=20, ge=1, le=5000),
    baseline_win_rate: float = Query(default=0.5, gt=0, lt=1),
    sigma_threshold: float = Query(default=3.0, gt=0, le=10),
    only_significant: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SuspiciousAccountSignal]:
    return _build_suspicious_signals(
        fresh_days=fresh_days,
        min_resolved=min_resolved,
        baseline_win_rate=baseline_win_rate,
        sigma_threshold=sigma_threshold,
        only_significant=only_significant,
        limit=limit,
    )
