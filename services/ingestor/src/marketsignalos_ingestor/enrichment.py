from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ComputedEnrichment:
    account_id: str
    # Fraction of all distinct markets this account has traded (0–1).
    cross_market_coordination: float
    # Win rate on fills placed within the pre-resolution window (0–1, 0 if too few).
    pre_resolution_accuracy: float
    # This account's share of total fill volume across all accounts (0–1).
    fill_intensity: float


@dataclass
class _AccountStats:
    markets: set[str]
    fill_count: int = 0
    # Resolved calls (market had an outcome)
    total_calls: int = 0
    total_wins: int = 0
    # Subset of calls placed within pre_resolution_window_hours before settlement
    pre_resolution_calls: int = 0
    pre_resolution_wins: int = 0

    def __init__(self) -> None:
        self.markets = set()
        self.fill_count = 0
        self.total_calls = 0
        self.total_wins = 0
        self.pre_resolution_calls = 0
        self.pre_resolution_wins = 0


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


def _clamp_01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def compute_enrichment(
    fills_path: Path,
    resolutions_path: Path,
    *,
    pre_resolution_window_hours: int = 24,
) -> list[ComputedEnrichment]:
    """Compute per-account enrichment signals from JSONL fills and resolutions.

    Three signals are produced:
    - cross_market_coordination: market breadth relative to the most active account.
    - pre_resolution_accuracy: win rate when trading within the pre-resolution window.
    - fill_intensity: share of total fill volume.
    """
    fills = _read_jsonl(fills_path)
    resolutions = _read_jsonl(resolutions_path)

    if not fills:
        return []

    # Build resolution index: market_ticker -> (settlement_side, resolved_at)
    resolved_by_market: dict[str, tuple[str, datetime]] = {}
    for row in resolutions:
        market = str(row["market_ticker"])
        side = str(row["settlement_side"]).lower()
        resolved_at = _parse_utc(str(row["resolved_at"]))
        resolved_by_market[market] = (side, resolved_at)

    by_account: dict[str, _AccountStats] = {}
    total_fills = len(fills)

    for row in fills:
        account_id = str(row["account_id"])
        market = str(row["market_ticker"])
        side = str(row["side"]).lower()
        traded_at = _parse_utc(str(row["traded_at"]))

        stats = by_account.setdefault(account_id, _AccountStats())
        stats.markets.add(market)
        stats.fill_count += 1

        resolution = resolved_by_market.get(market)
        if resolution is None:
            continue

        settlement_side, resolved_at = resolution
        won = 1 if side == settlement_side else 0
        stats.total_calls += 1
        stats.total_wins += won

        window_start = resolved_at - timedelta(hours=pre_resolution_window_hours)
        if window_start <= traded_at <= resolved_at:
            stats.pre_resolution_calls += 1
            stats.pre_resolution_wins += won

    if not by_account:
        return []

    max_markets = max(len(s.markets) for s in by_account.values())

    results: list[ComputedEnrichment] = []
    for account_id, stats in by_account.items():
        cross_market = len(stats.markets) / max_markets if max_markets > 0 else 0.0

        intensity = stats.fill_count / total_fills if total_fills > 0 else 0.0

        # Require at least 3 pre-resolution fills to compute accuracy reliably.
        if stats.pre_resolution_calls >= 3:
            pre_res_accuracy = stats.pre_resolution_wins / stats.pre_resolution_calls
        else:
            pre_res_accuracy = 0.0

        results.append(
            ComputedEnrichment(
                account_id=account_id,
                cross_market_coordination=round(_clamp_01(cross_market), 4),
                pre_resolution_accuracy=round(_clamp_01(pre_res_accuracy), 4),
                fill_intensity=round(_clamp_01(intensity), 6),
            )
        )

    return results
