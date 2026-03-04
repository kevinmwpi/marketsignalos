from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from marketsignalos_api.services.models import FillRecord, ResolutionRecord, SurfaceAccountStats


@dataclass(slots=True)
class _Aggregate:
    first_seen: datetime
    last_seen: datetime
    resolved_calls: int = 0
    wins: int = 0
    expected_wins: float = 0.0
    variance: float = 0.0


def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


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
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_resolutions.jsonl"


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []

    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned:
            rows.append(json.loads(cleaned))
    return rows


def _load_fills() -> list[FillRecord]:
    payloads = _load_jsonl(_fills_path())
    fills: list[FillRecord] = []
    for payload in payloads:
        fills.append(
            FillRecord(
                source=str(payload.get("source", "kalshi")),
                account_id=str(payload["account_id"]),
                market_ticker=str(payload["market_ticker"]),
                fill_id=str(payload["fill_id"]),
                trade_id=str(payload["trade_id"]),
                side=str(payload["side"]).lower(),
                price=float(payload["price"]),
                quantity=int(payload["quantity"]),
                traded_at=_parse_utc(str(payload["traded_at"])),
            )
        )
    return fills


def _load_resolutions() -> dict[str, ResolutionRecord]:
    payloads = _load_jsonl(_resolutions_path())
    latest: dict[str, ResolutionRecord] = {}

    for payload in payloads:
        record = ResolutionRecord(
            source=str(payload.get("source", "kalshi")),
            market_ticker=str(payload["market_ticker"]),
            resolved_at=_parse_utc(str(payload["resolved_at"])),
            settlement_side=str(payload["settlement_side"]).lower(),
        )
        current = latest.get(record.market_ticker)
        if current is None or record.resolved_at > current.resolved_at:
            latest[record.market_ticker] = record

    return latest


def _implied_probability(side: str, price: float) -> float:
    yes_price = min(max(price, 0.0), 100.0) / 100.0
    return yes_price if side == "yes" else 1.0 - yes_price


def collect_surface_account_stats(
    *,
    fresh_days: int,
    min_resolved: int,
) -> list[SurfaceAccountStats]:
    fills = _load_fills()
    resolutions = _load_resolutions()
    if not fills or not resolutions:
        return []

    reference_now = max(
        [*[(f.traded_at) for f in fills], *[(r.resolved_at) for r in resolutions.values()]]
    )
    min_activity = reference_now - timedelta(days=fresh_days)

    stats: dict[str, _Aggregate] = {}

    for fill in fills:
        resolution = resolutions.get(fill.market_ticker)
        if resolution is None or fill.traded_at < min_activity:
            continue

        account = stats.setdefault(
            fill.account_id,
            _Aggregate(
                first_seen=fill.traded_at,
                last_seen=fill.traded_at,
            ),
        )

        account.first_seen = min(account.first_seen, fill.traded_at)
        account.last_seen = max(account.last_seen, fill.traded_at)

        expected = _implied_probability(fill.side, fill.price)
        won = 1 if fill.side == resolution.settlement_side else 0

        account.resolved_calls += 1
        account.wins += won
        account.expected_wins += expected
        account.variance += expected * (1.0 - expected)

    output: list[SurfaceAccountStats] = []
    for account_id, aggregate in stats.items():
        resolved = aggregate.resolved_calls
        if resolved < min_resolved:
            continue

        wins = aggregate.wins
        expected = aggregate.expected_wins
        variance = aggregate.variance
        z_score = (wins - expected) / math.sqrt(variance) if variance > 0 else 0.0

        output.append(
            SurfaceAccountStats(
                account_id=account_id,
                first_seen=aggregate.first_seen,
                last_seen=aggregate.last_seen,
                resolved_calls=resolved,
                wins=wins,
                losses=resolved - wins,
                expected_wins=expected,
                win_rate=wins / resolved,
                z_score=z_score,
            )
        )

    output.sort(key=lambda row: (-row.z_score, -row.resolved_calls, row.account_id))
    return output
