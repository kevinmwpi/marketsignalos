"""
Gate 13's closing-line value under forecast-v5: the price 1 hour after each buy.

The horizon rule decided h = 1 hour on 2026-10-05 (blueprint §12, decision 6). The
build plan and the owner's decisions D1-D5 are in docs/gate13-clv-v5-plan.md.
For each BUY fill of a bet (wallet, condition, outcome):

    ref = YES price 1 h after the fill (entry_prices.price_after); NO = 1 - YES
    clv = ref - fill price, where the fill price is USDC / size: what the wallet paid (D3)

A bet's CLV is the USDC-weighted mean over its fills that have a reference, and its
weight in the wallet's event-capped aggregate is the USDC of those fills. Only fills
at least seven days before the market's scheduled end count (D1). That is the
population on which the horizon rule measured leakage, using the same filter as
``horizon_diagnostic``. Bets count whatever their status, resolved, exited or open
(D2), because the reference needs no resolution.

A fill without a reference is excluded and counted under its reason, never
zero-filled:

  - ``no_scheduled_end``: the market has no end date to apply the filter to;
  - ``near_scheduled_end``: the fill came less than seven days before that end;
  - ``invalid_fill``: USDC / size is not a price strictly between 0 and 1;
  - ``no_reference``: no price within ``price_after``'s tolerance an hour later.
    Either the window has not been fetched yet, or the market closed or went
    quiet first.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .closing_lines import ACTIVITY_FILE
from .entry_prices import CHUNK_SECONDS, ChunkKey, chunk_start, price_after
from .horizon_diagnostic import MARKETS_FILE, RULE, _rows, market_states

SCORE_VERSION = "forecast-v5"
HORIZON_SECONDS = 3600  # decision 6: h = 1 hour
MIN_SECONDS_TO_END = int(RULE["min_hours_to_scheduled_end"]) * 3600

# (timestamp, price paid, USDC) for one BUY fill
Fill = tuple[int, float, float]
# Sorted hourly YES series per condition id (entry_prices.load_entry_prices)
Series = Mapping[str, list[tuple[int, float]]]


@dataclass(frozen=True, slots=True)
class BetClv:
    """One bet's post-entry CLV. ``clv`` is None when no fill had a reference."""

    clv: float | None
    weight_usdc: float
    exclusions: Counter[str] = field(default_factory=Counter)


def fill_clv(
    fill: Fill, *, outcome_index: int, scheduled_end: int | None,
    points: list[tuple[int, float]],
) -> tuple[float | None, str]:
    """One fill's CLV, or None with the reason it is excluded (empty when it counts)."""
    ts, price, usdc = fill
    if scheduled_end is None:
        return None, "no_scheduled_end"
    if scheduled_end - ts < MIN_SECONDS_TO_END:
        return None, "near_scheduled_end"
    if not 0.0 < price < 1.0 or usdc <= 0.0:
        return None, "invalid_fill"
    yes = price_after(points, ts, HORIZON_SECONDS)
    if yes is None:
        return None, "no_reference"
    reference = yes if outcome_index == 0 else 1.0 - yes
    return reference - price, ""


def bet_clv(
    fills: Iterable[Fill], *, condition_id: str, outcome_index: int,
    scheduled_end: int | None, series: Series,
) -> BetClv:
    """Post-entry CLV of one bet from its BUY fills (see the module docstring)."""
    exclusions: Counter[str] = Counter()
    weighted = 0.0
    weight = 0.0
    points = series.get(condition_id, [])
    for fill in fills:
        clv, reason = fill_clv(fill, outcome_index=outcome_index,
                               scheduled_end=scheduled_end, points=points)
        if clv is None:
            exclusions[reason] += 1
            continue
        weighted += fill[2] * clv
        weight += fill[2]
    if weight <= 0.0:
        return BetClv(None, 0.0, exclusions)
    return BetClv(weighted / weight, weight, exclusions)


def priority_chunks(data_dir: Path) -> set[ChunkKey]:
    """Entry-price chunks forecast-v5 reads: the hour after every BUY fill placed at
    least seven days before its market's scheduled end, whatever the bet's status.
    The pilot's entry-price stage fetches them first, together with the horizon
    diagnostic's (approved 2026-10-05: open and exited bets were queuing behind
    newer buys)."""
    ends = {cid: state.scheduled_end for cid, state in
            market_states(data_dir / MARKETS_FILE).items() if state.scheduled_end is not None}
    chunks: set[ChunkKey] = set()
    for row in _rows(data_dir / ACTIVITY_FILE):  # streamed: activity is large
        if row.get("type") != "TRADE" or str(row.get("side", "")).upper() != "BUY":
            continue
        cid = str(row.get("condition_id", "")).strip().lower()
        ts = row.get("timestamp")
        end = ends.get(cid)
        if (end is None or not isinstance(ts, int) or isinstance(ts, bool)
                or end - ts < MIN_SECONDS_TO_END):
            continue
        for start in range(chunk_start(ts), ts + HORIZON_SECONDS + 1, CHUNK_SECONDS):
            chunks.add((cid, start))
    return chunks
