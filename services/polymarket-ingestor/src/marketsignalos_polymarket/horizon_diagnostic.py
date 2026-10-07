"""
Choose the post-entry horizon for gate 13's closing line (blueprint open decision 6).

Decision 6 settled the reference price as the market's price a fixed time *h* after
each buy; this diagnostic supplies the evidence for choosing *h*. For every candidate
horizon it reports, over bets on resolved binary markets:

  - coverage: bets with a reference price, bets still waiting on the backfill
    (``pending``), and bets with none (``no_reference``), split by reason: the
    market had closed by the horizon (``closed``, from Gamma's ``closedTime``), its
    fetched series stops before the horizon with no close time to explain it
    (``ended``), or the series continues past the horizon with no point within
    ``price_after``'s tolerance before it (``gap``);
  - leakage: the share of referenced bets whose reference already sits within 0.01
    of the bet's final value, beside the same share for the last pre-close price on
    the same bets (the reference rejected on 2026-09-30);
  - signal: the correlation between each bet's CLV and whether it won, with the mean
    CLV of winners and losers;
  - concentration: how many wallets the bets come from and the largest one's share.

The same numbers are repeated for the bets referenced at every horizon, so horizons
can be compared on one population.

Gate 13 pass counts are deliberately absent. *h* is chosen from leakage and coverage
before anyone sees how many wallets a horizon would qualify; the CLV-win correlation
is a sanity floor, not a target, because maximising agreement with winning is the
circularity decision 6 exists to avoid.

Only fills placed at least seven days before the market's scheduled end count
(``RULE["min_hours_to_scheduled_end"]``): the end date is known at entry, so the
filter adds no leakage. It was meant to keep markets that outlive every horizon, but
a scheduled end is not a close: on 2026-10-03, 95% of the kept bets whose market's
close was known had closed within seven days. The candidate horizons were cut to
1 h and 6 h for that reason; the filter itself stays as approved. The funnel reports
how many resolved bets the filter keeps, and how many of those, among markets whose
actual close time is known, closed within seven days of the bet anyway.

``priority_chunks`` lists the entry-price chunks these bets reach and
``priority_markets`` their markets; the lean pilot's entry-price and closing-line
stages fetch them before any other.

A bet is one (wallet, condition, outcome). Its CLV at *h* is the USDC-weighted mean,
over its BUY fills whose window has been fetched, of (reference - fill price), where
the reference is the outcome's price *h* after that fill and the fill price is USDC
divided by size (the activity store does not keep price). Prices are hourly series
values, so *h* = 1 hour means the first hourly point after the fill.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from . import closing_lines, entry_prices
from .closing_lines import ACTIVITY_FILE
from .entry_prices import CHUNK_SECONDS, FINAL_STATUSES, ChunkKey, chunk_start, price_after
from .jsonl_archive import iter_lines

# Candidate horizons. 24, 72 and 168 hours were dropped on 2026-10-03 with the
# owner's approval (blueprint decision 6, second amendment): 95% of the bets the
# rule keeps were on markets that closed within a week, so the common set could
# never reach the rule's sample at those horizons.
HORIZONS_HOURS = (1, 6)
NEAR_OUTCOME = 0.01
MARKETS_FILE = "polymarket_markets.jsonl"
REPORT_DIR = "diagnostics/horizon"
DECISION_FILE = "decision.json"

# The selection rule, approved by the owner on 2026-10-02 before any report existed
# (blueprint decision 6). Changing any of these needs a written justification there.
# min_hours_to_scheduled_end was added the same day, after the first report showed
# the common set could not fill: almost no market in the cohort lived seven days.
# Only fills placed at least that long before the market's scheduled end (known at
# entry, so no leakage) count. The candidate horizons (HORIZONS_HOURS) were cut to
# 1 h and 6 h on 2026-10-03; these values did not change.
RULE = {
    "min_hours_to_scheduled_end": 168,
    "min_common_bets": 500,
    "min_common_wallets": 20,
    "max_near_outcome": 0.05,
    "min_coverage": 0.5,
    "min_clv_win_corr": 0.0,  # strictly greater than
}

BetKey = tuple[str, str, int]  # (wallet, condition id, outcome index)


class MarketState(NamedTuple):
    closed: bool
    winner: int | None  # winning outcome index, when resolved
    scheduled_end: int | None  # Gamma's endDate, Unix seconds


@dataclass(slots=True)
class _Fill:
    timestamp: int
    price: float
    weight: float


@dataclass(slots=True)
class _HorizonTally:
    pending: int = 0
    no_reference: int = 0
    clv: list[float] = field(default_factory=list)
    won: list[int] = field(default_factory=list)
    near: list[float] = field(default_factory=list)
    near_preclose: list[float] = field(default_factory=list)
    wallets: list[str] = field(default_factory=list)
    missing: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))


@dataclass(slots=True)
class _BuyScan:
    fills: int = 0
    markets: set[str] = field(default_factory=set)
    resolved_bets: set[BetKey] = field(default_factory=set)  # before the end-date filter


def market_states(markets_path: Path) -> dict[str, MarketState]:
    """State per market in the store, the latest row winning. The winner follows
    the scorer's rule: Gamma says closed and exactly one of two outcome prices is at
    least 0.99; otherwise it is None."""
    latest: dict[str, dict[str, Any]] = {}
    for row in _rows(markets_path):
        cid = str(row.get("condition_id", "")).lower()
        if cid:
            latest[cid] = row
    states: dict[str, MarketState] = {}
    for cid, row in latest.items():
        closed = row.get("closed") is True
        prices = row.get("outcome_prices")
        winner = None
        if closed and isinstance(prices, list) and len(prices) == 2:
            try:
                won = [i for i, price in enumerate(prices) if float(price) >= 0.99]
            except (TypeError, ValueError):
                won = []
            winner = won[0] if len(won) == 1 else None
        end = closing_lines._parse_time(row.get("end_date"))
        states[cid] = MarketState(closed, winner, None if end is None else int(end.timestamp()))
    return states


def resolved_winners(markets_path: Path) -> dict[str, int]:
    """Winning outcome per resolved binary market in the store."""
    return {cid: state.winner for cid, state in market_states(markets_path).items()
            if state.winner is not None}


def resolved_bets(
    activity_path: Path, winners: dict[str, int], scan: _BuyScan | None = None, *,
    scheduled_ends: dict[str, int] | None = None, min_seconds_to_end: int = 0,
) -> dict[BetKey, list[_Fill]]:
    """BUY fills per bet, for bets on markets in ``winners``. With
    ``min_seconds_to_end``, only fills at least that long before the market's
    scheduled end count; a market without one is left out. ``scan`` collects every
    BUY fill, bought market and resolved bet for the coverage funnel."""
    bets: dict[BetKey, list[_Fill]] = defaultdict(list)
    for row in _rows(activity_path):
        if row.get("type") != "TRADE" or row.get("side") != "BUY":
            continue
        cid = str(row.get("condition_id", "")).strip().lower()
        if scan is not None and cid:
            scan.fills += 1
            scan.markets.add(cid)
        wallet = str(row.get("proxy_wallet", "")).lower()
        outcome = row.get("outcome_index")
        ts = row.get("timestamp")
        # The activity store keeps size and USDC, not price
        # (storage._ACTIVITY_JSONL_FIELDS), so the fill price is USDC / size.
        size = _float(row.get("size"))
        usdc = _float(row.get("usdc_size"))
        if (cid not in winners or not wallet or outcome not in (0, 1)
                or isinstance(ts, bool) or not isinstance(ts, int)
                or size is None or usdc is None or size <= 0 or usdc <= 0):
            continue
        price = usdc / size
        if not 0.0 < price < 1.0:
            continue
        key = (wallet, cid, int(outcome))
        if scan is not None:
            scan.resolved_bets.add(key)
        if min_seconds_to_end:
            end = (scheduled_ends or {}).get(cid)
            if end is None or end - ts < min_seconds_to_end:
                continue
        bets[key].append(_Fill(ts, price, usdc))
    return bets


def diagnose(
    data_dir: Path, *, now: datetime | None = None,
    horizons_hours: Iterable[int] = HORIZONS_HOURS,
) -> dict[str, Any]:
    """The full report for a pilot data directory. Reads only; writes nothing."""
    now = now or datetime.now(UTC)
    horizons = tuple(horizons_hours)
    scan = _BuyScan()
    states, winners, bets = _eligible_bets(data_dir, scan)
    entry_store = data_dir / entry_prices.STORE_DIR
    series = entry_prices.load_entry_prices(entry_store)
    receipts = entry_prices.latest_receipts(entry_store)
    final_chunks = {key for key, (status, _) in receipts.items() if status in FINAL_STATUSES}
    closing_store = data_dir / closing_lines.STORE_DIR
    preclose = closing_lines.load_closing_lines(closing_store).points
    closes = {**closing_lines.close_times(closing_store), **entry_prices.close_times(entry_store)}

    tallies = {h: _HorizonTally() for h in horizons}
    common: dict[int, _HorizonTally] = {h: _HorizonTally() for h in horizons}
    for (wallet, cid, outcome), fills in bets.items():
        won = int(winners[cid] == outcome)
        final_value = float(won)
        closing = preclose.get(cid)
        closing_ref = None if closing is None else _outcome_price(closing[1], outcome)
        per_h: dict[int, tuple[float, float]] = {}
        for h in horizons:
            seconds = h * 3600
            ready = [f for f in fills
                     if _window_fetched(cid, f.timestamp, seconds, final_chunks)]
            if not ready:
                tallies[h].pending += 1
                continue
            referenced = []
            for fill in ready:
                yes = price_after(series.get(cid, []), fill.timestamp, seconds)
                if yes is not None:
                    referenced.append((fill, _outcome_price(yes, outcome)))
            if not referenced:
                tallies[h].no_reference += 1
                first = min(fill.timestamp for fill in ready)
                tallies[h].missing[_missing_reason(series.get(cid, []), first, seconds,
                                                   closes.get(cid))] += 1
                continue
            total = sum(fill.weight for fill, _ in referenced)
            clv = sum(fill.weight * (ref - fill.price) for fill, ref in referenced) / total
            near = sum(fill.weight for fill, ref in referenced
                       if abs(ref - final_value) <= NEAR_OUTCOME) / total
            per_h[h] = (clv, near)
            _add(tallies[h], wallet, clv, won, near, closing_ref, final_value)
        if len(per_h) == len(horizons):
            for h, (clv, near) in per_h.items():
                _add(common[h], wallet, clv, won, near, closing_ref, final_value)

    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "near_outcome_threshold": NEAR_OUTCOME,
        "resolved_markets": len(winners),
        "funnel": _funnel(scan, states, closing_store, bets, closes),
        "resolved_bets": len(bets),
        "wallets": len({wallet for wallet, _, _ in bets}),
        "horizons": {f"{h}h": _summary(tallies[h], with_coverage=True) for h in horizons},
        "common": {
            "bets": len(common[horizons[0]].clv) if horizons else 0,
            **{f"{h}h": _summary(common[h], with_coverage=False) for h in horizons},
        },
    }
    report["selection"] = select_horizon(report, horizons)
    return report


def select_horizon(report: dict[str, Any], horizons: Iterable[int]) -> dict[str, Any]:
    """Apply ``RULE`` to a report: the longest horizon whose leakage is at most
    ``max_near_outcome`` both on the common set and on every bet referenced at that
    horizon, whose coverage (referenced over fetched, per horizon) is at least
    ``min_coverage`` and whose common-set CLV-win correlation is positive. Not
    ``eligible`` until the common set reaches the sample size.

    The per-horizon bound was added on 2026-10-03 (third amendment): the common set
    holds only bets whose market was still open at the longest horizon, a cleaner
    population than the one a chosen horizon would score."""
    common = report["common"]
    order = sorted(horizons, reverse=True)
    wallets = common[f"{order[0]}h"]["wallets"] if order else 0
    if common["bets"] < RULE["min_common_bets"] or wallets < RULE["min_common_wallets"]:
        return {"eligible": False,
                "reason": f"common set has {common['bets']} bets from {wallets} wallets; "
                          f"the rule needs {RULE['min_common_bets']} from "
                          f"{RULE['min_common_wallets']}"}
    failures: dict[str, str] = {}
    for h in order:
        key = f"{h}h"
        near = common[key]["near_outcome"]
        near_all = report["horizons"][key]["near_outcome"]
        corr = common[key]["clv_win_corr"]
        coverage = report["horizons"][key]["coverage"]
        if near is None or near > RULE["max_near_outcome"]:
            failures[key] = f"near_outcome {near}"
        elif near_all is None or near_all > RULE["max_near_outcome"]:
            failures[key] = f"near_outcome {near_all} on all referenced bets"
        elif coverage is None or coverage < RULE["min_coverage"]:
            failures[key] = f"coverage {coverage}"
        elif corr is None or corr <= RULE["min_clv_win_corr"]:
            failures[key] = f"clv_win_corr {corr}"
        else:
            return {"eligible": True, "horizon": key, "rejected_longer": failures}
    return {"eligible": True, "horizon": None, "rejected_longer": failures}


def priority_chunks(data_dir: Path) -> set[ChunkKey]:
    """Every entry-price chunk the diagnostic's bets reach, from each fill to the
    longest horizon after it: the chunks the decision is waiting on."""
    _, _, bets = _eligible_bets(data_dir)
    longest = max(HORIZONS_HOURS) * 3600
    return {(cid, start) for (_, cid, _), fills in bets.items() for fill in fills
            for start in range(chunk_start(fill.timestamp), fill.timestamp + longest + 1,
                               CHUNK_SECONDS)}


def priority_markets(data_dir: Path) -> set[str]:
    """The markets of the diagnostic's bets: the closing-line backfill fetches them
    first, which also records their actual close times."""
    _, _, bets = _eligible_bets(data_dir)
    return {cid for _, cid, _ in bets}


def run(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Lean-pilot stage: write the day's report and return it as the stage result.

    The first report whose selection is eligible is also written to
    ``decision.json`` and never replaced: the rule decides once, at the first
    sufficient sample, so later reports cannot be waited on for a preferred answer.
    """
    report = diagnose(data_dir, now=now)
    out = data_dir / REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / f"{report['generated_at'][:10]}.json", report)
    decision_path = out / DECISION_FILE
    if not decision_path.exists() and report["selection"]["eligible"]:
        _write_json(decision_path, {"decided_at": report["generated_at"], "rule": RULE,
                                    "horizons_hours": list(HORIZONS_HOURS),
                                    "near_outcome_bound_on": ["common", "horizons"],
                                    "selection": report["selection"], "report": report})
    decision = json.loads(decision_path.read_text(encoding="utf-8")) \
        if decision_path.exists() else None
    return {"status": "succeeded", **report,
            "decision": None if decision is None else {
                "decided_at": decision["decided_at"], **decision["selection"]}}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _eligible_bets(
    data_dir: Path, scan: _BuyScan | None = None,
) -> tuple[dict[str, MarketState], dict[str, int], dict[BetKey, list[_Fill]]]:
    """Market states, resolved winners and the bets the rule counts: fills placed at
    least ``min_hours_to_scheduled_end`` before the market's scheduled end."""
    states = market_states(data_dir / MARKETS_FILE)
    winners = {cid: state.winner for cid, state in states.items() if state.winner is not None}
    ends = {cid: state.scheduled_end for cid, state in states.items()
            if state.scheduled_end is not None}
    bets = resolved_bets(data_dir / ACTIVITY_FILE, winners, scan, scheduled_ends=ends,
                         min_seconds_to_end=int(RULE["min_hours_to_scheduled_end"]) * 3600)
    return states, winners, bets


def _missing_reason(series: list[tuple[int, float]], buy_ts: int, seconds: int,
                    closed_at: int | None) -> str:
    """Why ``price_after`` found no reference for a fill (see the module docstring)."""
    target = buy_ts + seconds
    if closed_at is not None and closed_at <= target:
        return "closed"
    if series and series[-1][0] > target:
        return "gap"
    return "ended"


def _funnel(scan: _BuyScan, states: dict[str, MarketState], closing_store: Path,
            bets: dict[BetKey, list[_Fill]], closes: dict[str, int]) -> dict[str, int]:
    """Where bought markets drop out before a bet can be scored: missing from the
    market store, stored but open, closed without a clear winner. The closing-line
    backfill asks Gamma for closed markets directly, so its receipts show how many
    bought markets Gamma already reports closed, independent of the store.

    The last two counts check the scheduled-end filter: among kept bets whose
    market's actual close is known, those it closed within seven days of the bet's
    first fill."""
    gamma_closed = {cid for cid, (status, _) in closing_lines.latest_receipts(closing_store)
                    .items() if status in ("ok", "no_history")}
    bought = scan.markets
    window = int(RULE["min_hours_to_scheduled_end"]) * 3600
    return {
        "buy_fills": scan.fills,
        "bought_markets": len(bought),
        "in_market_store": sum(cid in states for cid in bought),
        "closed_in_store": sum(states[cid].closed for cid in bought if cid in states),
        "resolved_in_store": sum(states[cid].winner is not None for cid in bought if cid in states),
        "closed_per_gamma_lookup": len(bought & gamma_closed),
        "resolved_bets": len(scan.resolved_bets),
        "bets_7d_before_scheduled_end": len(bets),
        "bets_7d_close_known": sum(cid in closes for _, cid, _ in bets),
        "bets_7d_closed_within_7d": sum(
            closes[cid] - min(fill.timestamp for fill in fills) < window
            for (_, cid, _), fills in bets.items() if cid in closes),
    }


def _window_fetched(cid: str, buy_ts: int, seconds: int, final: set[tuple[str, int]]) -> bool:
    """Every chunk from the buy to ``seconds`` after it has a final receipt."""
    return all((cid, start) in final for start in
               range(chunk_start(buy_ts), buy_ts + seconds + 1, CHUNK_SECONDS))


def _outcome_price(yes_price: float, outcome: int) -> float:
    return yes_price if outcome == 0 else 1.0 - yes_price


def _add(tally: _HorizonTally, wallet: str, clv: float, won: int, near: float,
         closing_ref: float | None, final_value: float) -> None:
    tally.clv.append(clv)
    tally.won.append(won)
    tally.near.append(near)
    tally.wallets.append(wallet)
    if closing_ref is not None:
        tally.near_preclose.append(float(abs(closing_ref - final_value) <= NEAR_OUTCOME))


def _summary(tally: _HorizonTally, *, with_coverage: bool) -> dict[str, Any]:
    n = len(tally.clv)
    counts: dict[str, int] = defaultdict(int)
    for wallet in tally.wallets:
        counts[wallet] += 1
    won = [c for c, w in zip(tally.clv, tally.won, strict=True) if w]
    lost = [c for c, w in zip(tally.clv, tally.won, strict=True) if not w]
    summary: dict[str, Any] = {
        "referenced": n,
        "near_outcome": _round(_mean(tally.near)),
        "preclose_bets": len(tally.near_preclose),
        "near_outcome_preclose": _round(_mean(tally.near_preclose)),
        "clv_win_corr": _round(_pearson(tally.clv, [float(w) for w in tally.won])),
        "clv_mean_won": _round(_mean(won)),
        "clv_mean_lost": _round(_mean(lost)),
        "wallets": len(counts),
        "top_wallet_share": _round(max(counts.values()) / n if n else None),
    }
    if with_coverage:
        fetched = n + tally.no_reference
        summary = {"pending": tally.pending, "no_reference": tally.no_reference,
                   "no_reference_reasons": dict(sorted(tally.missing.items())),
                   "coverage": _round(n / fetched if fetched else None), **summary}
    return summary


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _float(value: Any) -> float | None:
    """A finite JSON number as a float; anything else (bools, strings) is None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    for line in iter_lines(path):  # archive segments first (jsonl_archive)
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row
