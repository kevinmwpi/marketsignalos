"""
Gate 13 under forecast-v5: a read-only power diagnostic (plan step 3).

This is step 3 and the evidence for decision D4 in docs/gate13-clv-v5-plan.md:
whether ``MIN_CLV_SAMPLE`` measures the wallets or how long collection has run.
It changes no score and no threshold. From the pilot's current stores it reports:

  - per wallet, the forecast-v5 CLV statistics gate 13 reads (event-capped
    sample, mean and lower bound), the spread and effective sample size behind
    the bound, and the effective sample at which that bound would clear zero
    if the wallet kept its current mean and spread;
  - every excluded fill by reason, with ``no_reference`` split by the chunk
    receipts into ``not_ended`` (a chunk the hour needs has not ended yet, so it
    cannot be fetched: the structural lag of up to a week), ``fetch_failed`` (an
    ended chunk whose last attempt failed and waits for its retry),
    ``not_fetched`` (an ended chunk never tried: the real backlog), and
    horizon_diagnostic's ``closed``, ``ended`` and ``gap``;
  - gate-13 and tailable counts under forecast-v4 and forecast-v5. Both are
    scored now, from the same inputs, into a scratch directory that is deleted
    afterwards. These are the before/after counts plan step 4 requires.
    ``blocked_only_by_min_sample`` counts wallets whose only failed gate is
    gate 13's sample minimum while their CLV lower bound is already positive:
    the wallets ``MIN_CLV_SAMPLE`` alone keeps off the feed (decision D4). The
    report file lists them.

The per-wallet figures are recomputed here with the scorer's own functions
(``post_entry_clv.fill_clv``, the event-capped weights). They are checked
against the forecast-v5 enrichment rows, and any disagreement is counted in
``cross_check_mismatches``.
"""
from __future__ import annotations

import json
import math
import shutil
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import closing_lines, entry_prices
from .entry_prices import CHUNK_SECONDS, FINAL_STATUSES, SETTLE_SECONDS, ChunkKey, chunk_start
from .horizon_diagnostic import _missing_reason
from .post_entry_clv import HORIZON_SECONDS, fill_clv
from .price_lead import parse_iso_ts
from .runner import _iter_jsonl, _load_market_records
from .skill_computation import MIN_CLV_SAMPLE, _event_capped_weights, _weighted_clv_stats

REPORT_DIR = "diagnostics/gate13"
SCRATCH_DIR = ".gate13-scratch"
ACTIVITY_FILE = "polymarket_activity.jsonl"
MARKETS_FILE = "polymarket_markets.jsonl"
Z = 1.6448536269514722  # the one-sided 5% normal quantile the scorer uses

# (wallet, condition id, outcome index)
BetKey = tuple[str, str, int]


def wallet_stats(observations: list[tuple[float, str, float]]) -> dict[str, Any]:
    """Gate 13's figures for one wallet from (clv, event slug, weight) bets, plus
    the spread and effective sample behind the bound and the effective sample at
    which the bound would clear zero at this mean and spread (None when the mean
    is not positive: no sample size gets there)."""
    mean, lower, sample = _weighted_clv_stats(observations)
    weights = _event_capped_weights([(slug, weight) for _, slug, weight in observations])
    total = sum(weights)
    sum_sq = sum(w * w for w in weights)
    n_eff = total * total / sum_sq if sum_sq > 0 else 0.0
    sd = (math.sqrt(sum(w * (clv - mean) ** 2 for w, (clv, _, _) in zip(
        weights, observations, strict=True)) / total) if total > 0 else 0.0)
    needed = (Z * sd / mean) ** 2 if mean > 0 else None
    return {"bets": len(observations), "sample": round(sample, 4), "n_eff": round(n_eff, 4),
            "mean": round(mean, 6), "sd": round(sd, 6), "lower_bound": round(lower, 6),
            "passes": sample >= MIN_CLV_SAMPLE and lower > 0.0,
            "needed_n_eff": None if needed is None else round(needed, 1)}


def diagnose(data_dir: Path, *, v5_rows: dict[str, dict[str, Any]] | None = None,
             observed_before: datetime | None = None) -> dict[str, Any]:
    """Per-wallet v5 CLV statistics and exclusion reasons for a data directory,
    reading entry prices fetched by ``observed_before`` as the scorer does."""
    markets: dict[str, Any] = {}
    for market in _load_market_records(data_dir / MARKETS_FILE):
        known = markets.get(market.condition_id)
        if known is None or market.fetched_at > known.fetched_at:
            markets[market.condition_id] = market
    bets: dict[BetKey, tuple[str, list[tuple[int, float, float]]]] = {}
    for row in _iter_jsonl(data_dir / ACTIVITY_FILE):  # streamed: activity is large
        if row.get("type") != "TRADE" or str(row.get("side", "")).upper() != "BUY":
            continue
        cid = str(row.get("condition_id", ""))
        outcome = row.get("outcome_index")
        size, usdc, ts = row.get("size"), row.get("usdc_size"), row.get("timestamp")
        if (not cid or outcome not in (0, 1) or cid not in markets
                or not isinstance(ts, int) or isinstance(ts, bool)
                or not isinstance(size, (int, float)) or not isinstance(usdc, (int, float))
                or size <= 0 or usdc <= 0):
            continue
        key = (str(row.get("proxy_wallet", "")).lower(), cid, int(outcome))
        slug, fills = bets.setdefault(key, (str(row.get("event_slug", "")), []))
        fills.append((ts, float(usdc) / float(size), float(usdc)))

    store = data_dir / entry_prices.STORE_DIR
    series = entry_prices.load_entry_prices(store, observed_before=observed_before)
    receipts = {key: status for key, (status, _) in entry_prices.latest_receipts(store).items()}
    now = int((observed_before or datetime.now(UTC)).timestamp())
    closes = {**closing_lines.close_times(data_dir / closing_lines.STORE_DIR),
              **entry_prices.close_times(store)}

    exclusions: Counter[str] = Counter()
    by_wallet: dict[str, list[tuple[float, str, float]]] = defaultdict(list)
    for (wallet, cid, outcome), (slug, fills) in bets.items():
        end = parse_iso_ts(markets[cid].end_date) or None
        points = series.get(cid, [])
        weighted = weight = 0.0
        for fill in fills:
            clv, reason = fill_clv(fill, outcome_index=outcome, scheduled_end=end,
                                   points=points)
            if clv is None:
                if reason == "no_reference":
                    reason = _no_reference_reason(cid, fill[0], points, receipts,
                                                  closes.get(cid), now=now)
                exclusions[reason] += 1
                continue
            weighted += fill[2] * clv
            weight += fill[2]
        if weight > 0:
            by_wallet[wallet].append((weighted / weight, slug, weight))

    wallets = {wallet: wallet_stats(obs) for wallet, obs in sorted(by_wallet.items())}
    mismatches = 0
    for wallet, row in (v5_rows or {}).items():
        mine = wallets.get(wallet)
        theirs = (row.get("clv_mean", 0.0), row.get("clv_lower_bound", 0.0),
                  row.get("clv_sample_size", 0.0))
        ours = ((mine["mean"], mine["lower_bound"], mine["sample"]) if mine
                else (0.0, 0.0, 0.0))
        if any(abs(a - b) > 1e-4 for a, b in zip(ours, theirs, strict=True)):
            mismatches += 1
    return {"wallets": wallets, "exclusions": dict(sorted(exclusions.items())),
            "cross_check_mismatches": mismatches}


def run(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Lean-pilot stage: score v4 and v5 into a scratch directory, diagnose, write
    the day's report, and return its summary. Nothing outside the scratch
    directory and the report is written."""
    from .score_snapshot import score_snapshot

    now = now or datetime.now(UTC)
    scratch = data_dir / SCRATCH_DIR
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        counts: dict[str, dict[str, int]] = {}
        v5_rows: dict[str, dict[str, Any]] = {}
        min_sample_only: list[str] = []
        for version in ("forecast-v4", "forecast-v5"):
            score_snapshot(data_dir, scratch, version.replace("forecast-", ""),
                           score_version=version)
            rows = [json.loads(line) for line in _lines(
                scratch / version.replace("forecast-", "") / "polymarket_wallet_enrichment.jsonl")]
            counts[version] = _counts(rows)
            if version == "forecast-v5":
                v5_rows = {str(row["proxy_wallet"]).lower(): row for row in rows}
                min_sample_only = sorted(wallet for wallet, row in v5_rows.items()
                                         if _blocked_only_by_min_sample(row))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    report = diagnose(data_dir, v5_rows=v5_rows, observed_before=now)
    summary = {"generated_at": now.isoformat(), "min_clv_sample": MIN_CLV_SAMPLE,
               "counts": counts, **_summary(report)}
    out = data_dir / REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now.date().isoformat()}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({**summary, "wallets": report["wallets"],
                               "v5_blocked_only_by_min_sample": min_sample_only},
                              indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return {"status": "succeeded", **summary}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _no_reference_reason(cid: str, ts: int, points: list[tuple[int, float]],
                         receipts: dict[ChunkKey, str], closed_at: int | None, *,
                         now: int) -> str:
    """Why a fill has no price an hour later. Chunks the hour needs that are not
    final come first, by what the backfill can do about them (see the module
    docstring); otherwise horizon_diagnostic's reason for the fetched series."""
    window = range(chunk_start(ts), ts + HORIZON_SECONDS + 1, CHUNK_SECONDS)
    open_starts = [start for start in window
                   if receipts.get((cid, start)) not in FINAL_STATUSES]
    if any(start + CHUNK_SECONDS > now - SETTLE_SECONDS for start in open_starts):
        return "not_ended"  # entry_prices.select_pending waits for these
    if any((cid, start) in receipts for start in open_starts):
        return "fetch_failed"
    if open_starts:
        return "not_fetched"
    return _missing_reason(points, ts, HORIZON_SECONDS, closed_at)


def _blocked_only_by_min_sample(row: dict[str, Any]) -> bool:
    """Gate 13's sample minimum is the wallet's only failed gate, and its CLV
    lower bound is already positive (the scorer checks the minimum first)."""
    reasons = list(row.get("tailability_reasons", []))
    return (len(reasons) == 1
            and reasons[0].startswith(f"fewer than {int(MIN_CLV_SAMPLE)} ")
            and _is_clv_reason(reasons[0])
            and float(row.get("clv_lower_bound", 0.0)) > 0.0)


def _is_clv_reason(reason: str) -> bool:
    """A tailability reason gate 13 gave (either score version's wording)."""
    return "closing-line" in reason or "post-entry CLV" in reason


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    def gate13(row: dict[str, Any]) -> bool:
        return (float(row.get("clv_sample_size", 0.0)) >= MIN_CLV_SAMPLE
                and float(row.get("clv_lower_bound", 0.0)) > 0.0)

    reasons = [list(row.get("tailability_reasons", [])) for row in rows]
    return {
        "wallets": len(rows),
        "gate13_pass": sum(gate13(row) for row in rows),
        "tailable": sum(row.get("tailability_status") == "tailable" for row in rows),
        "blocked_only_by_gate13": sum(bool(r) and all(_is_clv_reason(x) for x in r)
                                      for r in reasons),
        "blocked_only_by_min_sample": sum(_blocked_only_by_min_sample(row) for row in rows),
    }


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    wallets = list(report["wallets"].values())
    with_spread = [w for w in wallets if w["n_eff"] >= 2]
    positive = [w for w in wallets if w["needed_n_eff"] is not None]

    def median(values: list[float]) -> float | None:
        return round(statistics.median(values), 6) if values else None

    return {
        "wallets_with_observations": len(wallets),
        "wallets_sample_at_least_min": sum(w["sample"] >= MIN_CLV_SAMPLE for w in wallets),
        "median_sample": median([w["sample"] for w in wallets]),
        "median_mean": median([w["mean"] for w in with_spread]),
        "median_sd": median([w["sd"] for w in with_spread]),
        "wallets_positive_mean": len(positive),
        "median_needed_n_eff": median([w["needed_n_eff"] for w in positive]),
        "wallets_needing_at_most_current_n_eff": sum(
            w["needed_n_eff"] <= w["n_eff"] for w in positive),
        "exclusions": report["exclusions"],
        "cross_check_mismatches": report["cross_check_mismatches"],
    }


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
