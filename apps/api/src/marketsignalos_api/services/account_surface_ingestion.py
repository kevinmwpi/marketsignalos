from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from marketsignalos_api.services.leaderboard_models import SurfaceAccountStats


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _fills_path() -> Path:
    configured = os.getenv("INGESTOR_FILLS_STORE_PATH")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_fills.jsonl"


def _resolutions_path() -> Path:
    configured = os.getenv("INGESTOR_RESOLUTIONS_STORE_PATH")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_market_resolutions.jsonl"


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned:
            rows.append(json.loads(cleaned))
    return rows


@dataclass(slots=True)
class _Aggregate:
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_calls: int = 0
    wins: int = 0
    expected_wins: float = 0.0
    variance: float = 0.0


def _expected_probability(side: str, price: float) -> float:
    yes_probability = min(max(price, 0.0), 100.0) / 100.0
    return yes_probability if side.lower() == "yes" else 1.0 - yes_probability


def collect_surface_account_stats(*, fresh_days: int, min_resolved: int) -> list[SurfaceAccountStats]:
    fills = _read_jsonl(_fills_path())
    resolutions = _read_jsonl(_resolutions_path())
    if not fills or not resolutions:
        return []

    resolved_by_market: dict[str, str] = {
        str(row["market_ticker"]): str(row["settlement_side"]).lower() for row in resolutions
    }
    reference_now = max(_parse_utc(str(row["traded_at"])) for row in fills)
    min_activity = reference_now - timedelta(days=fresh_days)

    by_account: dict[str, _Aggregate] = {}

    for row in fills:
        market = str(row["market_ticker"])
        account_id = str(row["account_id"])
        side = str(row["side"]).lower()
        traded_at = _parse_utc(str(row["traded_at"]))

        if traded_at < min_activity:
            continue
        settlement_side = resolved_by_market.get(market)
        if settlement_side is None:
            continue

        aggregate = by_account.setdefault(
            account_id,
            _Aggregate(first_seen_at=traded_at, last_seen_at=traded_at),
        )
        aggregate.first_seen_at = min(aggregate.first_seen_at, traded_at)
        aggregate.last_seen_at = max(aggregate.last_seen_at, traded_at)

        expected = _expected_probability(side=side, price=float(row["price"]))
        won = 1 if side == settlement_side else 0

        aggregate.resolved_calls += 1
        aggregate.wins += won
        aggregate.expected_wins += expected
        aggregate.variance += expected * (1.0 - expected)

    output: list[SurfaceAccountStats] = []
    for account_id, aggregate in by_account.items():
        if aggregate.resolved_calls < min_resolved:
            continue
        z_score = (
            (aggregate.wins - aggregate.expected_wins) / math.sqrt(aggregate.variance)
            if aggregate.variance > 0
            else 0.0
        )
        output.append(
            SurfaceAccountStats(
                account_id=account_id,
                first_seen_at=aggregate.first_seen_at,
                last_seen_at=aggregate.last_seen_at,
                resolved_calls=aggregate.resolved_calls,
                wins=aggregate.wins,
                expected_wins=aggregate.expected_wins,
                z_score=z_score,
            )
        )

    output.sort(key=lambda row: (-row.z_score, -row.resolved_calls, row.account_id))
    return output

