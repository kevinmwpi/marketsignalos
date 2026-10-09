"""Stage 3 step 6: the cohort-v1 evaluation, computed once after the evaluation date.

Written and frozen before the window opens (docs/stage3-cohort-v1-plan.md), so the
analysis is pre-registered with the selection rule: the package hash in the frozen
config's score generation covers this file. It refuses to run before the evaluation
date (no peeking), and the pilot runs it once, ``EVAL_LAG`` after that date, so the
last signals' price windows (step 3) have been fetched.

**Signals.** Rows of ``cohort-v1/signals.jsonl`` with the frozen config hash, captured
in frozen mode, detected in ``[window.opens, window.evaluation_date)``. Excluded rows
are counted by reason, never priced.

**Primary outcome (S3).** For each captured signal, the price of the token bought one
hour after detection minus the follower's all-in price (the $100 clip's VWAP plus the
taker fee, step 2). The price at a horizon is the last ``/prices-history`` point at
or before it, at most ``TOLERANCE_SECONDS`` earlier; with none, the signal's outcome
is missing, with its reason. Signals in one event (event slug, else market) are
averaged first, and events weigh equally. The confidence interval resamples events
(``BOOTSTRAP_DRAWS`` draws, fixed seed).

**Primary test (S5).** T2 against zero. ``edge_after_costs`` when the one-sided 95%
lower bound is above zero. Otherwise ``inconclusive`` if the frozen window was
declared under-powered, else ``null`` (kill criterion 2).

**Secondary, all reported whatever they show.** The 6 h horizon; the same outcomes
before fees; T1, the polled part of T3, the matched comparison set and T2 minus it;
the CLV-only ablation; T2's settlement ROI (unresolved in the analysed data:
censored); T2's calibration, Brier score and log loss, for the model's forecast
(the wallet's entry price moved by its frozen posterior edge) and for the market's mid
at detection; and whether excluded signals moved differently from captured ones.

**Collection.** How completely the pilot collected during the window, from its run
and recovery receipts, so that signals that never existed are not mistaken for none
(see ``collection``).
"""
from __future__ import annotations

import bisect
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from .closing_lines import _parse_time, _read_jsonl
from .cohort_capture import MAX_DETECTION_LAG_SECONDS
from .cohort_prices import load_signal_prices
from .cohort_v1 import config_hash

STAGE_DIR = "cohort-v1"
SIGNALS_FILE = "signals.jsonl"
RESULT_FILE = "result.json"
MARKETS_FILE = "polymarket_markets.jsonl"
ENRICHMENT = "polymarket_wallet_enrichment.jsonl"
CONTROL_DIR = ".lean-pilot"  # lean_pilot's run receipts and pilot_recovery's receipts
EVAL_LAG = timedelta(hours=24)  # step 3 fetches a window 7 h after detection, 6-hourly
HORIZONS = {"1h": 3600, "6h": 6 * 3600}
TOLERANCE_SECONDS = 15 * 60
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 20261007
CALIBRATION_BINS = 10
PROBABILITY_EPS = 1e-3


def evaluation_date(config: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(config["window"]["evaluation_date"]))


def is_due(config: dict[str, Any], now: datetime) -> bool:
    return now >= evaluation_date(config) + EVAL_LAG


# ── Per-signal outcomes ──────────────────────────────────────────────────────

def price_at(series: list[tuple[int, float]], ts: int,
             tolerance: int = TOLERANCE_SECONDS) -> float | None:
    """The last point at or before ``ts``, if it is at most ``tolerance`` earlier."""
    index = bisect.bisect_right(series, (ts, math.inf)) - 1
    if index < 0 or ts - series[index][0] > tolerance:
        return None
    return series[index][1]


def _event(signal: dict[str, Any]) -> str:
    return str(signal.get("event_slug") or signal.get("condition_id") or "")


def signal_outcomes(signal: dict[str, Any],
                    series: list[tuple[int, float]] | None) -> dict[str, Any]:
    """Net and gross improvement at each horizon, or the reason one is missing."""
    detected = _parse_time(signal.get("detected_at"))
    clip = signal.get("clip") or {}
    out: dict[str, Any] = {}
    for name, seconds in HORIZONS.items():
        if detected is None or not clip:
            out[name] = {"missing": "no detection time or clip"}
            continue
        if not series:
            out[name] = {"missing": "no price window"}
            continue
        price = price_at(series, int(detected.timestamp()) + seconds)
        if price is None:
            out[name] = {"missing": f"no price within {TOLERANCE_SECONDS // 60} min "
                                    f"before +{name}"}
            continue
        out[name] = {"net": price - float(clip["all_in_price"]),
                     "gross": price - float(clip["vwap"])}
    return out


# ── Event-weighted means and the event-cluster bootstrap ─────────────────────

def event_means(values: Iterable[tuple[str, float]]) -> list[float]:
    by_event: dict[str, list[float]] = defaultdict(list)
    for event, value in values:
        by_event[event].append(value)
    return [sum(v) / len(v) for _, v in sorted(by_event.items())]


def _quantile(sorted_values: list[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _draws(means: list[float], rng: random.Random) -> list[float]:
    return [sum(rng.choices(means, k=len(means))) / len(means)
            for _ in range(BOOTSTRAP_DRAWS)]


def summarize(values: list[tuple[str, float]], *, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Event-weighted mean, 95% interval, one-sided 95% lower bound and the share of
    draws at or below zero."""
    means = event_means(values)
    if not means:
        return {"signals": 0, "events": 0, "mean": None}
    boot = sorted(_draws(means, random.Random(seed)))
    return {"signals": len(values), "events": len(means), "mean": sum(means) / len(means),
            "ci95": [_quantile(boot, 0.025), _quantile(boot, 0.975)],
            "lower95_one_sided": _quantile(boot, 0.05),
            "share_of_draws_at_or_below_zero": sum(d <= 0 for d in boot) / len(boot)}


def difference(a: list[tuple[str, float]], b: list[tuple[str, float]], *,
               seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Event-weighted mean of ``a`` minus that of ``b``, each resampled on its own."""
    ma, mb = event_means(a), event_means(b)
    if not ma or not mb:
        return {"mean": None, "events": [len(ma), len(mb)]}
    rng = random.Random(seed)
    boot = sorted(x - y for x, y in zip(_draws(ma, rng), _draws(mb, rng), strict=True))
    return {"mean": sum(ma) / len(ma) - sum(mb) / len(mb), "events": [len(ma), len(mb)],
            "ci95": [_quantile(boot, 0.025), _quantile(boot, 0.975)],
            "lower95_one_sided": _quantile(boot, 0.05)}


# ── Inputs ───────────────────────────────────────────────────────────────────

def window_signals(rows: Iterable[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    opens = datetime.fromisoformat(str(config["window"]["opens"]))
    ends = evaluation_date(config)
    selected = []
    for row in rows:
        detected = _parse_time(row.get("detected_at"))
        if (row.get("config_hash") == config["config_hash"]
                and row.get("membership_mode") == "frozen"
                and detected is not None and opens <= detected < ends):
            selected.append(row)
    return selected


def groups(config: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Wallet sets by the frozen config. Only polled wallets have signals, so T3 is
    the part of it that was polled."""
    tiers = config["tiers"]
    comparison = frozenset(config["comparison"]["pairs"].values())
    clv_only = frozenset(config.get("ablations", {}).get("clv_only", []))
    t2 = frozenset(tiers["T2"])
    return {"T1": frozenset(tiers["T1"]), "T2": t2,
            "T3_polled": frozenset(tiers["T3"]) & (t2 | comparison | clv_only),
            "comparison": comparison, "clv_only": clv_only}


def resolutions(data_dir: Path) -> dict[str, int | None]:
    """condition id -> winning outcome index (None: closed without one winner), from the
    latest row per market; the signal ledger's rule. Unresolved markets are absent."""
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(data_dir / MARKETS_FILE):
        cid = str(row.get("condition_id", "")).lower()
        if cid and str(row.get("fetched_at", "")) >= str(latest.get(cid, {}).get(
                "fetched_at", "")):
            latest[cid] = row
    out: dict[str, int | None] = {}
    for cid, row in latest.items():
        prices = row.get("outcome_prices")
        if not row.get("closed") or not isinstance(prices, list):
            continue
        winners = [i for i, p in enumerate(prices)
                   if isinstance(p, (int, float)) and float(p) >= 0.99]
        out[cid] = winners[0] if len(winners) == 1 else None
    return out


def edge_means(data_dir: Path, config: dict[str, Any]) -> dict[str, float]:
    """Each wallet's posterior edge (log-odds) in the frozen score generation."""
    generation = config["score_generation"]
    path = data_dir / "score-snapshots" / str(generation["run_id"]) / ENRICHMENT
    return {str(row.get("proxy_wallet", "")).lower(): float(row.get("edge_mean") or 0.0)
            for row in _read_jsonl(path)}


def _logit(p: float) -> float:
    p = min(max(p, PROBABILITY_EPS), 1.0 - PROBABILITY_EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


def scores(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """Brier score and log loss of forecasts against 0/1 outcomes."""
    if not pairs:
        return {"n": 0}
    clamp = [(min(max(f, PROBABILITY_EPS), 1.0 - PROBABILITY_EPS), y) for f, y in pairs]
    return {"n": len(pairs),
            "brier": sum((f - y) ** 2 for f, y in clamp) / len(clamp),
            "log_loss": -sum(math.log(f) if y else math.log(1.0 - f)
                             for f, y in clamp) / len(clamp)}


def calibration_bins(pairs: list[tuple[float, int]]) -> list[dict[str, Any]]:
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for f, y in pairs:
        bins[min(int(f * CALIBRATION_BINS), CALIBRATION_BINS - 1)].append((f, y))
    return [{"bin": [b / CALIBRATION_BINS, (b + 1) / CALIBRATION_BINS], "n": len(rows),
             "mean_forecast": sum(f for f, _ in rows) / len(rows),
             "observed_rate": sum(y for _, y in rows) / len(rows)}
            for b, rows in sorted(bins.items())]


# ── Collection coverage ──────────────────────────────────────────────────────

def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def collection(data_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """How completely the pilot collected during the window, from its receipts.

    A member's fill becomes a signal only if a collection stores it and that run's
    capture reads it within ``MAX_DETECTION_LAG_SECONDS``. What escapes is counted:

    - ``stale_fills``: fills first seen too late, summed over the capture results;
    - ``capture_by_status``: capture passes, by status (a failed pass lost its signals);
    - ``collections_stopped``: collections that stopped before finishing, which are
      the runs ``pilot_recovery`` repaired. Fills such a run had stored are never read
      by a later capture, and how many there were is unknown;
    - gaps between the starts of finished collections, and the window's ends. Fills go
      stale only in a gap longer than the lag.

    Runs count by start time in ``[opens, evaluation_date)``.
    """
    opens = datetime.fromisoformat(str(config["window"]["opens"]))
    ends = evaluation_date(config)
    control = data_dir / CONTROL_DIR
    started_by_run: dict[str, datetime] = {}
    starts: list[datetime] = []
    partial = stale = unreadable = 0
    capture: Counter[str] = Counter()
    for path in sorted((control / "runs").glob("*/receipt.json")):
        receipt = _json_object(path)
        started = _parse_time(receipt.get("started_at")) if receipt else None
        if receipt is None or started is None:
            unreadable += 1
            continue
        started_by_run[path.parent.name] = started
        stages = receipt.get("stages")
        collect = stages.get("collect") if isinstance(stages, dict) else None
        if not opens <= started < ends or not isinstance(collect, dict):
            continue
        starts.append(started)
        if collect.get("status") == "partial":
            partial += 1
        result = collect.get("result")
        passed = result.get("cohort_v1_capture") if isinstance(result, dict) else None
        if not isinstance(passed, dict):
            capture["missing"] += 1
            continue
        fills = passed.get("stale_fills")
        if isinstance(fills, int):
            stale += fills
        detail = passed.get("reason") or passed.get("error_type")
        status = str(passed.get("status", "missing"))
        capture[f"{status}: {detail}" if detail else status] += 1
    stopped = sum(
        1 for path in (control / "recoveries").glob("*.json")
        if (started := started_by_run.get(path.stem)) is not None and opens <= started < ends)
    gaps = [(b - a).total_seconds() for a, b in pairwise([opens, *sorted(starts), ends])]
    return {
        "window_hours": round((ends - opens).total_seconds() / 3600, 1),
        "collections": len(starts),
        "collections_partial": partial,
        "collections_stopped": stopped,
        "longest_gap_hours": round(max(gaps) / 3600, 2),
        "gaps_over_detection_lag": sum(gap > MAX_DETECTION_LAG_SECONDS for gap in gaps),
        "stale_fills": stale,
        "capture_by_status": dict(sorted(capture.items())),
        "unreadable_receipts": unreadable,
    }


# ── The evaluation ───────────────────────────────────────────────────────────

def evaluate(data_dir: Path, config: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    if config_hash(config) != config.get("config_hash"):
        raise ValueError("frozen cohort config does not match its hash")
    if now < evaluation_date(config):
        raise ValueError("before the evaluation date: no peeking")
    stage = data_dir / STAGE_DIR
    signals = window_signals(_read_jsonl(stage / SIGNALS_FILE), config)
    series = load_signal_prices(data_dir)
    sets = groups(config)
    resolved = resolutions(data_dir)
    edges = edge_means(data_dir, config)

    excluded = Counter(reason for s in signals if s.get("status") != "captured"
                       for reason in s.get("exclusion_reasons") or [])
    captured = [s for s in signals if s.get("status") == "captured"]
    outcomes = {str(s["signal_id"]): signal_outcomes(s, series.get(str(s["signal_id"])))
                for s in captured}
    missing = Counter(f"{h}: {o[h]['missing']}" for o in outcomes.values()
                      for h in HORIZONS if "missing" in o[h])

    def values(wallets: frozenset[str], horizon: str, kind: str) -> list[tuple[str, float]]:
        return [(_event(s), outcomes[str(s["signal_id"])][horizon][kind]) for s in captured
                if str(s.get("wallet", "")).lower() in wallets
                and kind in outcomes[str(s["signal_id"])][horizon]]

    by_group = {name: {f"{h}_{kind}": summarize(values(wallets, h, kind))
                       for h in HORIZONS for kind in ("net", "gross")}
                for name, wallets in sets.items()}
    primary = by_group["T2"]["1h_net"]
    under_powered = bool(config["window"].get("under_powered"))
    lower = primary.get("lower95_one_sided")
    verdict = ("edge_after_costs" if lower is not None and lower > 0
               else "inconclusive" if under_powered else "null")

    # The primary tier's settlement ROI, censored when unresolved, and its
    # calibration against the market's mid at detection.
    roi: list[tuple[str, float]] = []
    model: list[tuple[float, int]] = []
    market: list[tuple[float, int]] = []
    censored = 0
    for s in captured:
        if str(s.get("wallet", "")).lower() not in sets["T2"]:
            continue
        cid = str(s.get("condition_id", "")).lower()
        if cid not in resolved:
            censored += 1
            continue
        winner = resolved[cid]
        all_in = float(s["clip"]["all_in_price"])
        if winner is None:
            roi.append((_event(s), 0.0))
            continue
        won = int(winner == int(s["outcome_index"]))
        roi.append((_event(s), (1.0 - all_in) / all_in if won else -1.0))
        shares = float(s.get("wallet_shares") or 0.0)
        book = s.get("book") or {}
        if shares > 0:
            vwap = float(s.get("wallet_usdc") or 0.0) / shares
            edge = edges.get(str(s.get("wallet", "")).lower(), 0.0)
            model.append((_sigmoid(_logit(vwap) + edge), won))
        ask, bid = book.get("best_ask"), book.get("best_bid")
        if isinstance(ask, (int, float)):
            market.append(((ask + bid) / 2 if isinstance(bid, (int, float)) else ask, won))

    # Did excluded signals move differently? Mark-to-mark over 1 h, no costs.
    moves: dict[str, list[tuple[str, float]]] = {"captured": [], "excluded": []}
    for s in signals:
        detected = _parse_time(s.get("detected_at"))
        points = series.get(str(s.get("signal_id")))
        if detected is None or not points:
            continue
        start = price_at(points, int(detected.timestamp()))
        end = price_at(points, int(detected.timestamp()) + HORIZONS["1h"])
        if start is not None and end is not None:
            moves["captured" if s.get("status") == "captured" else "excluded"].append(
                (_event(s), end - start))

    return {
        "cohort_id": config["cohort_id"], "config_hash": config["config_hash"],
        "window": config["window"], "computed_at": now.astimezone(UTC).isoformat(),
        "verdict": verdict, "primary": {"tier": "T2", "horizon": "1h", **primary},
        "signals": {"in_window": len(signals), "captured": len(captured),
                    "excluded_by_reason": dict(sorted(excluded.items())),
                    "outcome_missing_by_reason": dict(sorted(missing.items()))},
        "groups": by_group,
        "t2_minus_comparison": {h: difference(values(sets["T2"], h, "net"),
                                              values(sets["comparison"], h, "net"))
                                for h in HORIZONS},
        "ablations": {"price_only": 0.0, "clv_only": by_group["clv_only"]["1h_net"],
                      "historical_edge": by_group["T2"]["1h_net"],
                      "combined": by_group["T1"]["1h_net"]},
        "settlement_roi": {**summarize(roi), "censored": censored},
        "calibration": {"model": {**scores(model), "bins": calibration_bins(model)},
                        "market_mid": {**scores(market), "bins": calibration_bins(market)}},
        "exclusion_check_1h_move": {k: summarize(v) for k, v in moves.items()},
        "collection": collection(data_dir, config),
        "method": {"bootstrap_draws": BOOTSTRAP_DRAWS, "seed": BOOTSTRAP_SEED,
                   "tolerance_seconds": TOLERANCE_SECONDS, "horizons": HORIZONS},
    }


def write_once(data_dir: Path, result: dict[str, Any]) -> Path:
    path = data_dir / STAGE_DIR / RESULT_FILE
    with path.open("x", encoding="utf-8") as out:  # one look
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return path
