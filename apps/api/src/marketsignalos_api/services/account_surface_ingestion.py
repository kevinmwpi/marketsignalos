from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from marketsignalos_api._paths import fills_store_path, resolutions_store_path
from marketsignalos_api.services.leaderboard_models import SurfaceAccountStats


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


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


def _coerce_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError("price must be numeric")


def _aggregate_fills(
    fills: list[dict[str, object]],
    resolved_by_market: dict[str, str],
    min_activity: datetime,
) -> dict[str, _Aggregate]:
    by_account: dict[str, _Aggregate] = {}
    for row in fills:
        market = str(row["market_ticker"])
        account_id = str(row["account_id"])
        side = str(row["side"]).lower()

        traded_at_raw = row["traded_at"]
        if isinstance(traded_at_raw, datetime):
            traded_at = traded_at_raw if traded_at_raw.tzinfo else traded_at_raw.replace(tzinfo=timezone.utc)
        else:
            traded_at = _parse_utc(str(traded_at_raw))

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

        expected = _expected_probability(side=side, price=_coerce_float(row["price"]))
        won = 1 if side == settlement_side else 0

        aggregate.resolved_calls += 1
        aggregate.wins += won
        aggregate.expected_wins += expected
        aggregate.variance += expected * (1.0 - expected)
    return by_account


def _build_output(
    by_account: dict[str, _Aggregate],
    min_resolved: int,
) -> list[SurfaceAccountStats]:
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


def _collect_from_jsonl(
    *, fresh_days: int, min_resolved: int
) -> list[SurfaceAccountStats]:
    from datetime import timedelta

    fills = _read_jsonl(fills_store_path())
    resolutions = _read_jsonl(resolutions_store_path())
    if not fills or not resolutions:
        return []

    resolved_by_market: dict[str, str] = {
        str(row["market_ticker"]): str(row["settlement_side"]).lower()
        for row in resolutions
    }
    reference_now = max(_parse_utc(str(row["traded_at"])) for row in fills)
    min_activity = reference_now - timedelta(days=fresh_days)

    by_account = _aggregate_fills(fills, resolved_by_market, min_activity)
    return _build_output(by_account, min_resolved)


def _collect_from_db(
    *, fresh_days: int, min_resolved: int
) -> list[SurfaceAccountStats]:
    from datetime import datetime, timezone

    from marketsignalos_api.db import db_cursor

    with db_cursor() as cur:
        cur.execute(  # type: ignore[union-attr]
            """
            SELECT
                f.account_id,
                f.market_ticker,
                f.side,
                f.price::float          AS price,
                f.traded_at             AS traded_at,
                mr.settlement_side
            FROM fills f
            INNER JOIN market_resolutions mr
                ON mr.source = f.source
               AND mr.market_ticker = f.market_ticker
            WHERE f.traded_at >= now() - make_interval(days => %s)
            ORDER BY f.traded_at
            """,
            (fresh_days,),
        )
        rows: list[dict[str, object]] = [dict(r) for r in cur.fetchall()]  # type: ignore[union-attr]

    if not rows:
        return []

    # Build resolution map and a sentinel min_activity from the query results.
    resolved_by_market: dict[str, str] = {}
    min_dt = datetime.max.replace(tzinfo=timezone.utc)
    max_dt = datetime.min.replace(tzinfo=timezone.utc)

    for row in rows:
        resolved_by_market[str(row["market_ticker"])] = str(row["settlement_side"]).lower()
        traded_at = row["traded_at"]
        if isinstance(traded_at, datetime):
            if traded_at.tzinfo is None:
                traded_at = traded_at.replace(tzinfo=timezone.utc)
            min_dt = min(min_dt, traded_at)
            max_dt = max(max_dt, traded_at)

    # All rows already filtered by the WHERE clause; use epoch as min_activity floor.
    min_activity = datetime.min.replace(tzinfo=timezone.utc)
    by_account = _aggregate_fills(rows, resolved_by_market, min_activity)
    return _build_output(by_account, min_resolved)


def collect_surface_account_stats(
    *, fresh_days: int, min_resolved: int
) -> list[SurfaceAccountStats]:
    from marketsignalos_api.db import get_database_url

    if get_database_url():
        return _collect_from_db(fresh_days=fresh_days, min_resolved=min_resolved)
    return _collect_from_jsonl(fresh_days=fresh_days, min_resolved=min_resolved)
