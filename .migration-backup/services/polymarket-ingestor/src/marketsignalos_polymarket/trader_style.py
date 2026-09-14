"""
Deterministic trader-style classification.

Classifies HOW a wallet trades — "systematic" (an automation-shaped
operational footprint) vs "discretionary" (human-paced) — from the same
activity stream the skill model already consumes. The archetype is purely
descriptive and never gates tailability: a "systematic" label means the
wallet's observable cadence looks automated, nothing more. Language is
intentionally non-accusatory (no "bot detection" claims).

Five sub-signals, each scored 0..1 behind an availability guard, then
averaged:

  tempo      trades per active day              (saturates at 50/day)
  cadence    median seconds between trades      (<=60s -> 1, >=1h -> 0, log scale)
  coverage   distinct UTC hours-of-day traded   (<=12h -> 0, 24h -> 1; humans sleep)
  sizing     share of BUYs at a top-3 modal size (needs >=20 buys)
  breadth    distinct markets per active day    (saturates at 10/day)

automation_score is the mean of the available sub-scores. The archetype label
requires >=50 lifetime trades AND >=3 available sub-signals; otherwise the
wallet is "unclassified". Every available sub-signal contributes one
human-readable driver string, so the label always decomposes into observable
facts.

Style descriptors that are NOT automation evidence — top category + share,
exited-position share, median exit hold hours — ride along for the wallet
dossier but never feed the score (a politics-only human is just as
concentrated as a sports bot).
"""
from __future__ import annotations

import itertools
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .models import PolymarketActivity

# Classification guards: with fewer trades or sub-signals than this the
# archetype stays "unclassified" (the raw metrics are still reported).
MIN_TRADES_FOR_CLASSIFICATION = 50
MIN_SUBSIGNALS_FOR_CLASSIFICATION = 3

# automation_score cutoffs for the label.
SYSTEMATIC_THRESHOLD = 0.65
DISCRETIONARY_THRESHOLD = 0.35

# Per-sub-signal calibration. Values chosen against the reference systematic
# wallet (hundreds of trades/day, sub-minute gaps, 24h coverage) and typical
# discretionary wallets (a few trades/day, waking-hours only).
_TEMPO_SATURATION_TRADES_PER_DAY = 50.0
_CADENCE_FLOOR_SECONDS = 60.0     # median gap at/below this scores 1
_CADENCE_CEILING_SECONDS = 3600.0  # median gap at/above this scores 0
_MIN_GAPS_FOR_CADENCE = 20
_COVERAGE_FLOOR_HOURS = 12         # <=12 distinct UTC hours scores 0
_MIN_TRADES_FOR_COVERAGE = 48      # 2 trades/hour-slot before coverage means anything
_MIN_BUYS_FOR_SIZING = 20
_MODAL_SIZE_TOP_N = 3
_BREADTH_SATURATION_MARKETS_PER_DAY = 10.0


@dataclass(frozen=True, slots=True)
class TraderStyle:
    """One wallet's style classification + the raw metrics behind it."""

    archetype: str  # "systematic" | "mixed" | "discretionary" | "unclassified"
    automation_score: float  # 0..1 mean of available sub-scores
    drivers: list[str] = field(default_factory=list)
    # Raw sub-signal inputs (0 / "" when not computable).
    trades_per_active_day: float = 0.0
    median_intertrade_gap_seconds: float = 0.0  # 0 = too few trades to measure
    active_utc_hours: int = 0  # distinct UTC hours-of-day with >=1 trade (0-24)
    buy_size_uniformity: float = 0.0  # share of buys at a top-3 modal cent-size
    markets_per_active_day: float = 0.0
    # Style descriptors (reported, never scored).
    top_category: str = ""
    top_category_share: float = 0.0  # of trades with a known category
    exited_position_share: float = 0.0  # positions fully sold before resolution
    median_exit_hold_hours: float = 0.0  # first BUY -> last SELL on exited positions


def unclassified_style(reason: str) -> TraderStyle:
    return TraderStyle(archetype="unclassified", automation_score=0.0, drivers=[reason])


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _cadence_score(median_gap_seconds: float) -> float:
    """1 at sub-minute medians, 0 at hour-plus, log-linear between — the gap
    between '22s' and '3min' matters more than between '40min' and '55min'."""
    if median_gap_seconds <= _CADENCE_FLOOR_SECONDS:
        return 1.0
    if median_gap_seconds >= _CADENCE_CEILING_SECONDS:
        return 0.0
    span = math.log(_CADENCE_CEILING_SECONDS) - math.log(_CADENCE_FLOOR_SECONDS)
    return (math.log(_CADENCE_CEILING_SECONDS) - math.log(median_gap_seconds)) / span


def _format_gap(seconds: float) -> str:
    if seconds < 120.0:
        return f"{seconds:.0f}s"
    if seconds < 7200.0:
        return f"{seconds / 60.0:.0f}m"
    return f"{seconds / 3600.0:.1f}h"


@dataclass(slots=True)
class _PositionFlow:
    """Buy/sell share flow for one (condition, outcome) leg."""

    first_buy_ts: int = 0
    last_sell_ts: int = 0
    bought: float = 0.0
    sold: float = 0.0


def _exit_descriptors(trades: list[PolymarketActivity]) -> tuple[float, float]:
    """(exited_position_share, median_exit_hold_hours). A position counts as
    exited when cumulative sells cover cumulative buys; hold time runs from
    the first BUY to the last SELL."""
    flows: dict[tuple[str, int], _PositionFlow] = {}
    for event in trades:
        if not event.condition_id:
            continue
        flow = flows.setdefault(
            (event.condition_id, event.outcome_index), _PositionFlow()
        )
        side = event.side.upper()
        if side == "BUY":
            flow.bought += event.size
            if flow.first_buy_ts == 0 or event.timestamp < flow.first_buy_ts:
                flow.first_buy_ts = event.timestamp
        elif side == "SELL":
            flow.sold += event.size
            flow.last_sell_ts = max(flow.last_sell_ts, event.timestamp)

    considered = [flow for flow in flows.values() if flow.bought > 0.0]
    if not considered:
        return 0.0, 0.0
    exited = [
        flow
        for flow in considered
        if flow.sold >= flow.bought - 1e-9
        and flow.first_buy_ts > 0
        and flow.last_sell_ts >= flow.first_buy_ts
    ]
    share = len(exited) / len(considered)
    if not exited:
        return round(share, 4), 0.0
    hold_hours = statistics.median(
        (flow.last_sell_ts - flow.first_buy_ts) / 3600.0 for flow in exited
    )
    return round(share, 4), round(hold_hours, 2)


def _category_descriptors(
    trades: list[PolymarketActivity],
    categories_by_condition: dict[str, str],
) -> tuple[str, float]:
    """(top_category, share of known-category trades in it)."""
    counts: Counter[str] = Counter()
    for event in trades:
        category = categories_by_condition.get(event.condition_id, "")
        if category:
            counts[category] += 1
    if not counts:
        return "", 0.0
    top_category, top_count = counts.most_common(1)[0]
    return top_category, round(top_count / sum(counts.values()), 4)


def compute_trader_style(
    activity: list[PolymarketActivity],
    *,
    categories_by_condition: dict[str, str] | None = None,
) -> TraderStyle:
    """Compute one wallet's style from its raw activity stream.

    Deterministic: derived entirely from trade timestamps, sizes, and market
    identifiers — no wall clock, no randomness.
    """
    trades = sorted(
        (e for e in activity if e.type == "TRADE" and e.timestamp > 0),
        key=lambda e: e.timestamp,
    )
    if not trades:
        return unclassified_style("no trades observed")

    trade_count = len(trades)
    active_days = len({e.timestamp // 86400 for e in trades})

    scores: list[float] = []
    drivers: list[str] = []

    # Tempo — always available once there is at least one trade.
    trades_per_day = trade_count / active_days
    scores.append(_clamp01(trades_per_day / _TEMPO_SATURATION_TRADES_PER_DAY))
    drivers.append(f"{trades_per_day:.1f} trades per active day")

    # Cadence — median inter-trade gap.
    median_gap = 0.0
    gaps = [b.timestamp - a.timestamp for a, b in itertools.pairwise(trades)]
    if len(gaps) >= _MIN_GAPS_FOR_CADENCE:
        median_gap = float(statistics.median(gaps))
        scores.append(_cadence_score(median_gap))
        drivers.append(f"median {_format_gap(median_gap)} between trades")

    # Coverage — distinct UTC hours-of-day with at least one trade.
    active_hours = len({(e.timestamp % 86400) // 3600 for e in trades})
    if trade_count >= _MIN_TRADES_FOR_COVERAGE:
        scores.append(
            _clamp01((active_hours - _COVERAGE_FLOOR_HOURS) / (24.0 - _COVERAGE_FLOOR_HOURS))
        )
        drivers.append(f"active in {active_hours}/24 UTC hours")

    # Sizing — share of buys at one of the wallet's top-3 modal cent-sizes.
    buy_sizes = [
        round(e.usdc_size, 2)
        for e in trades
        if e.side.upper() == "BUY" and e.usdc_size > 0.0
    ]
    uniformity = 0.0
    if len(buy_sizes) >= _MIN_BUYS_FOR_SIZING:
        size_counts = Counter(buy_sizes)
        modal = sum(count for _, count in size_counts.most_common(_MODAL_SIZE_TOP_N))
        uniformity = modal / len(buy_sizes)
        scores.append(uniformity)
        drivers.append(f"{uniformity:.0%} of buys at a repeated size")

    # Breadth — distinct markets touched per active day.
    distinct_markets = len({e.condition_id for e in trades if e.condition_id})
    markets_per_day = distinct_markets / active_days
    scores.append(_clamp01(markets_per_day / _BREADTH_SATURATION_MARKETS_PER_DAY))
    drivers.append(f"{markets_per_day:.1f} distinct markets per active day")

    automation_score = sum(scores) / len(scores)
    if (
        trade_count < MIN_TRADES_FOR_CLASSIFICATION
        or len(scores) < MIN_SUBSIGNALS_FOR_CLASSIFICATION
    ):
        archetype = "unclassified"
        automation_score = 0.0
        drivers = [
            (
                "insufficient history for style classification "
                f"({trade_count} trades, {len(scores)} style signals)"
            )
        ]
    elif automation_score >= SYSTEMATIC_THRESHOLD:
        archetype = "systematic"
    elif automation_score <= DISCRETIONARY_THRESHOLD:
        archetype = "discretionary"
    else:
        archetype = "mixed"

    exited_share, median_hold_hours = _exit_descriptors(trades)
    top_category, top_share = _category_descriptors(
        trades, categories_by_condition or {}
    )

    return TraderStyle(
        archetype=archetype,
        automation_score=round(automation_score, 4),
        drivers=drivers,
        trades_per_active_day=round(trades_per_day, 2),
        median_intertrade_gap_seconds=round(median_gap, 1),
        active_utc_hours=active_hours,
        buy_size_uniformity=round(uniformity, 4),
        markets_per_active_day=round(markets_per_day, 2),
        top_category=top_category,
        top_category_share=top_share,
        exited_position_share=exited_share,
        median_exit_hold_hours=median_hold_hours,
    )
