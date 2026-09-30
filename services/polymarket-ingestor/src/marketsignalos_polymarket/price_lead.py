"""
Price-lead scoring: does the market tend to follow this wallet's entries?

A wallet worth tailing at low latency is one whose entries are quickly
followed by the price converging toward its side — its fills carry
information the market hasn't priced yet. This module scores that
footprint deterministically from data the pipeline already collects
(price snapshots, resolved bets, market end dates). Three explainable
sub-signals, each with a raw value and a 0–1 score:

  post-entry drift   weighted mean move of the picked outcome's price from
                     the wallet's entry VWAP to the first observation within
                     48h of its last fill. Positive = the market moves
                     toward the wallet's side shortly after it enters.
  longshot           weighted win rate on entries at <= 25¢ versus the
  conversion         market-implied rate (the mean entry price). Winning
                     far above implied on longshots is the classic
                     informed-entry footprint.
  late-entry         weighted win rate on bets entered within 72h of the
  accuracy           market's end date versus implied. Accuracy that spikes
                     close to resolution suggests the wallet acts on
                     information late-arriving to the market.

`price_lead_score` is the mean of the available sub-scores. All weights are
event-capped (one vote per event, split by at-risk capital) to match the
skill fit. Like the trader-style archetype this is DESCRIPTIVE ONLY — it
labels how the market reacts to a wallet, never gates tailability, and
makes no claim about *why* the wallet is early.

Snapshot sparsity: the drift sub-signal needs a price observation inside
the 48h window, which only accrues while scheduled ingest is running.
Wallets without qualifying observations simply skip that sub-signal.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

# condition_id -> [(fetched_at_iso, yes_price, closed)] sorted by fetched_at.
# Structurally identical to skill_computation._PriceHistory (redeclared here
# to keep the import direction one-way: skill_computation imports this module).
PriceSeries = dict[str, list[tuple[str, float, bool]]]

DRIFT_WINDOW_HOURS = 48.0
DRIFT_MIN_SAMPLES = 3.0          # weighted bets needed to trust the drift mean
DRIFT_FULL_SCORE = 0.10          # +10¢ mean 48h drift scores 1.0
LONGSHOT_MAX_ENTRY = 0.25
LATE_ENTRY_WINDOW_HOURS = 72.0
MIN_INDEPENDENT_EVENTS = 5.0     # weighted events needed for the rate signals
EXCESS_FULL_SCORE = 0.25         # winning 25pts above implied scores 1.0


def parse_iso_ts(iso: str) -> int:
    """ISO-8601 → unix seconds; 0 when blank or unparseable. Naive stamps
    are treated as UTC so the score never depends on the host timezone."""
    if not iso:
        return 0
    try:
        # fromisoformat has understood a trailing "Z" since 3.11; the package
        # targets 3.12, so the old .replace() shim is dead weight.
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


@dataclass(frozen=True, slots=True)
class LeadBet:
    """One betting record, reduced to what price-lead scoring needs."""

    condition_id: str
    outcome_index: int
    event_slug: str
    entry_price: float   # buys-only VWAP
    cost_usdc: float
    entry_ts: int        # unix seconds of the last fill
    won: bool | None     # None = market not resolved (drift still measurable)
    market_end_ts: int   # unix seconds of the market's end date; 0 unknown


@dataclass(frozen=True, slots=True)
class PriceLead:
    """Per-wallet price-lead result. All rates are event-capped weighted."""

    price_lead_score: float          # 0..1 mean of available sub-scores
    drivers: list[str] = field(default_factory=list)
    post_entry_drift_48h: float = 0.0     # mean picked-side move, $/share
    post_entry_drift_samples: float = 0.0  # weighted bets with a 48h observation
    longshot_win_rate: float = 0.0
    longshot_implied_rate: float = 0.0
    longshot_events: float = 0.0
    late_entry_win_rate: float = 0.0
    late_entry_implied_rate: float = 0.0
    late_entry_events: float = 0.0


def unmeasured_price_lead(reason: str) -> PriceLead:
    return PriceLead(price_lead_score=0.0, drivers=[reason])


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _event_capped_weights(items: list[tuple[str, float]]) -> list[float]:
    """One total vote per event, split across legs by at-risk capital —
    the same capping rule the Bayesian skill fit applies."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, (event_slug, _cost) in enumerate(items):
        grouped[event_slug or f"__blank_event_{index}"].append(index)

    weights = [0.0] * len(items)
    for indexes in grouped.values():
        total_cost = sum(max(0.0, items[i][1]) for i in indexes)
        even_weight = 1.0 / len(indexes)
        for i in indexes:
            weights[i] = (
                max(0.0, items[i][1]) / total_cost if total_cost > 0.0 else even_weight
            )
    return weights


def _picked_price(yes_price: float, outcome_index: int) -> float:
    return yes_price if outcome_index == 0 else 1.0 - yes_price


def _first_observation_within(
    series: list[tuple[str, float, bool]], *, after_ts: int, window_seconds: float
) -> float | None:
    """YES price of the first open-market observation in (after_ts,
    after_ts + window]. None when nothing was observed inside the window."""
    for fetched_at, yes_price, closed in series:
        if closed:
            continue
        obs_ts = parse_iso_ts(fetched_at)
        if obs_ts <= after_ts:
            continue
        if obs_ts - after_ts > window_seconds:
            return None  # series is sorted — nothing later can be in-window
        return yes_price
    return None


def _weighted_rate(
    bets: list[LeadBet],
) -> tuple[float, float, float]:
    """(win_rate, implied_rate, weighted_events) over resolved bets."""
    resolved = [b for b in bets if b.won is not None]
    if not resolved:
        return 0.0, 0.0, 0.0
    weights = _event_capped_weights([(b.event_slug, b.cost_usdc) for b in resolved])
    total = sum(weights)
    if total <= 0.0:
        return 0.0, 0.0, 0.0
    win_rate = sum(w for w, b in zip(weights, resolved) if b.won) / total
    implied = sum(w * b.entry_price for w, b in zip(weights, resolved)) / total
    return win_rate, implied, total


def compute_price_lead(bets: list[LeadBet], history: PriceSeries) -> PriceLead:
    """Score one wallet's price-lead footprint from its betting records and
    the per-condition price series (markets + snapshots)."""
    usable = [b for b in bets if 0.0 < b.entry_price < 1.0]
    if not usable:
        return unmeasured_price_lead("no bets with a usable entry price")

    scores: list[float] = []
    drivers: list[str] = []

    # 1. Post-entry drift within 48h of the last fill.
    drift_obs: list[tuple[float, str, float]] = []  # (drift, event_slug, cost)
    for bet in usable:
        series = history.get(bet.condition_id)
        if not series or bet.entry_ts <= 0:
            continue
        yes_price = _first_observation_within(
            series, after_ts=bet.entry_ts, window_seconds=DRIFT_WINDOW_HOURS * 3600.0
        )
        if yes_price is None:
            continue
        drift = _picked_price(yes_price, bet.outcome_index) - bet.entry_price
        drift_obs.append((drift, bet.event_slug, bet.cost_usdc))
    drift_mean = 0.0
    drift_samples = 0.0
    if drift_obs:
        weights = _event_capped_weights([(slug, cost) for _, slug, cost in drift_obs])
        drift_samples = sum(weights)
        if drift_samples > 0.0:
            drift_mean = (
                sum(w * d for w, (d, _, _) in zip(weights, drift_obs)) / drift_samples
            )
    if drift_samples >= DRIFT_MIN_SAMPLES:
        scores.append(_clamp01(drift_mean / DRIFT_FULL_SCORE))
        drivers.append(
            f"picked side moves {drift_mean * 100.0:+.1f}c within 48h of entry "
            f"({drift_samples:.1f} weighted bets)"
        )

    # 2. Longshot conversion (entries at <= 25c).
    longshots = [b for b in usable if b.entry_price <= LONGSHOT_MAX_ENTRY]
    ls_win, ls_implied, ls_events = _weighted_rate(longshots)
    if ls_events >= MIN_INDEPENDENT_EVENTS:
        scores.append(_clamp01((ls_win - ls_implied) / EXCESS_FULL_SCORE))
        drivers.append(
            f"wins {ls_win * 100.0:.0f}% of sub-25c entries "
            f"(market implied {ls_implied * 100.0:.0f}%, {ls_events:.1f} events)"
        )

    # 3. Late-entry accuracy (entered within 72h of the market's end date).
    late = [
        b
        for b in usable
        if b.market_end_ts > 0
        and b.entry_ts > 0
        and 0 <= b.market_end_ts - b.entry_ts <= LATE_ENTRY_WINDOW_HOURS * 3600.0
    ]
    late_win, late_implied, late_events = _weighted_rate(late)
    if late_events >= MIN_INDEPENDENT_EVENTS:
        scores.append(_clamp01((late_win - late_implied) / EXCESS_FULL_SCORE))
        drivers.append(
            f"wins {late_win * 100.0:.0f}% of entries made within 72h of close "
            f"(implied {late_implied * 100.0:.0f}%, {late_events:.1f} events)"
        )

    if not scores:
        return PriceLead(
            price_lead_score=0.0,
            drivers=["insufficient price/resolution data to measure"],
            post_entry_drift_48h=round(drift_mean, 6),
            post_entry_drift_samples=round(drift_samples, 4),
            longshot_win_rate=round(ls_win, 6),
            longshot_implied_rate=round(ls_implied, 6),
            longshot_events=round(ls_events, 4),
            late_entry_win_rate=round(late_win, 6),
            late_entry_implied_rate=round(late_implied, 6),
            late_entry_events=round(late_events, 4),
        )

    return PriceLead(
        price_lead_score=round(sum(scores) / len(scores), 6),
        drivers=drivers,
        post_entry_drift_48h=round(drift_mean, 6),
        post_entry_drift_samples=round(drift_samples, 4),
        longshot_win_rate=round(ls_win, 6),
        longshot_implied_rate=round(ls_implied, 6),
        longshot_events=round(ls_events, 4),
        late_entry_win_rate=round(late_win, 6),
        late_entry_implied_rate=round(late_implied, 6),
        late_entry_events=round(late_events, 4),
    )
