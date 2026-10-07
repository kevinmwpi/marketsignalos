"""
Cohort v1's tiers, comparison set and frozen configuration (Stage 3 step 1).

docs/stage3-cohort-v1-plan.md, decisions S1-S7, chosen by the owner on 2026-10-06.
From one published score generation this module builds:

- **Tiers (S1), nested.**
  - T1: tailable, all 13 gates.
  - T2: the six data gates pass, and every remaining tailability reason belongs to
    gate 13 (gates 1-12). **Primary.**
  - T3: the six data gates pass (``data_quality_status == "trusted"``).
  Every other screened wallet is listed with its reasons. So are wallets the cohort
  stage excluded, and leaderboard wallets never screened because the watchlist was
  full.
- **The comparison set (S5).** For each T2 member, in address order, one T3
  non-member matched at the cutoff:
  1. same ``top_category`` and the same tercile of buys in the 30 days before the
     cutoff;
  2. else the same tercile;
  3. else the nearest buy count.
  Ties go to the lower address. The remaining imbalance is reported.
- **The frozen configuration**: the screened population, the score generation it
  came from (run id, manifest hash, code hash), gate thresholds, the outcome, the cost
  model, the baseline and the matching rule. Its ``config_hash`` is the SHA-256 of the
  canonical JSON without that key. The window and evaluation date stay ``None`` until
  the power count (step 5).
- **The member list** that the pilot polls every hour (S2, step 4): T2 plus the
  comparison set. It is written as its own file so a provisional list can run during
  the burn-in and be replaced at the freeze without a code change.

Nothing here reads an outcome. The 30-day buy counts are activity *before* the cutoff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cohort import excluded_wallets
from .jsonl_archive import iter_lines
from .score_snapshot import ENRICHMENT, load_generation
from .skill_computation import MIN_CLV_SAMPLE, MIN_RECENT_INDEPENDENT_EVENTS

log = logging.getLogger("marketsignalos.polymarket.cohort_v1")

COHORT_ID = "cohort-v1"
PRIMARY_TIER = "T2"
ACTIVITY_FILE = "polymarket_activity.jsonl"
LEADERBOARD_FILE = "polymarket_leaderboard.jsonl"
MEMBERS_FILE = "cohort_v1_members.json"
ACTIVITY_DAYS = 30
STAGE_DIR = "cohort-v1"  # on the pilot volume: members.json, frozen-config.json, state.json
TARGET_EVENTS = 100  # S6: about 100 independent events detect +0.5c at sigma 2c
MAX_WINDOW_DAYS = 42
POWER_LOOKBACK_DAYS = 14

# The gates as the scorer applies them (skill_computation._enrichment_from_rollup).
# The frozen code hash pins the exact behaviour; this records it for readers.
GATES = {
    "1-6 data": "activity, positions, closed positions, all-time and 30-day "
                "economics complete; market metadata coverage 1.0",
    "7 ess": ">= 20 independent settled events",
    "8 posterior_skill": ">= 0.80",
    "9 edge_lower_bound": "> 0",
    "10 economics": "all-time PnL > 0, all-time ROI > 0, 30-day PnL >= 0",
    "11 recent_ess": f">= {MIN_RECENT_INDEPENDENT_EVENTS:g}",
    "12 recent edge": "recent edge mean >= 0",
    "13 clv": f"sample >= {MIN_CLV_SAMPLE:g} and lower bound > 0 "
              "(1 h after each buy, forecast-v5)",
}

# Decisions S3-S5, frozen verbatim with the cohort.
OUTCOME = {
    "primary": "event-weighted signed price improvement for the follower, 1 h after "
               "the follower's entry, net of measured costs",
    "secondary": ["settlement ROI (unresolved at the evaluation date: censored)",
                  "calibration, Brier score and log loss", "the 6 h horizon",
                  ("the same 1 h improvement before fees, to tell no edge from an edge "
                   "the fees consume")],
    "price_reference": ("the token bought, CLOB /prices-history at 5-minute fidelity over "
                        "detection - 1 h to + 6 h: the last point at or before detection + "
                        "1 h (+ 6 h), at most 15 minutes before it"),
    "event_weighting": "signals in one event share one event weight, as gate 13",
    "analysis": "event-cluster bootstrap, one look at the evaluation date",
}
COST_MODEL = {
    "signal": "a member's BUY fills of one (market, outcome) first seen by one pilot "
              "collection; fills more than 3 h old when seen are not signals",
    "entry_time": "the first pilot run that sees the wallet's buy",
    "entry_price": "best ask for the side bought, in that run",
    "size_usdc": 100,
    "slippage": "walk the asks for 100 USDC of notional; the fee comes on top",
    "fees": "taker fee as Polymarket's clob-client-v2 charges it: shares * r * "
            "(p * (1 - p)) ** e at each level's price, r and e from CLOB "
            "/clob-markets fd in the same run; no fd means no fee, as that client does",
    "all_in_price": "(100 + fee) / shares",
    "missing": "a signal without a readable book or fee details, with fee or token "
               "sources (CLOB vs Gamma) that disagree, too thin for the clip, or on a "
               "market not accepting orders is excluded with its reason, never priced "
               "at zero cost",
}
BASELINE = {
    "primary_test": "T2 follower improvement after costs against zero "
                    "(price-only baseline)",
    "secondary": "T2 against the matched comparison set, same polling schedule",
    "per_wallet_claims": False,
}
MATCHING = ("per T2 member in address order: an unused T3 non-member with the same "
            "top_category and 30-day buy tercile, else the same tercile, else the "
            "nearest buy count; ties to the lower address")
# Blueprint section 6 Stage 3 asks for these ablations in the result. Each is the
# primary outcome on its own wallets; the CLV-only wallets are polled hourly with the
# members so that their signals exist (added 2026-10-07, before the freeze).
ABLATIONS = {
    "price_only": "zero: buying at the market price expects no improvement",
    "clv_only": "data gates 1-6 and gate 13, whatever gates 7-12 say",
    "historical_edge": "T2: gates 1-12, gate 13 not required",
    "combined": "T1: all 13 gates",
}


def passes_clv_only(row: dict[str, Any]) -> bool:
    """The CLV-only ablation's rule: data trusted, and no gate-13 reason."""
    reasons = [str(reason) for reason in row.get("tailability_reasons") or []]
    return (row.get("tailability_status") == "tailable"
            or (row.get("data_quality_status") == "trusted"
                and not any(_is_gate13(reason) for reason in reasons)))


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    """(tier, reasons) for one enrichment row; the tier is T1, T2, T3 or none."""
    reasons = [str(reason) for reason in row.get("tailability_reasons") or []]
    if row.get("tailability_status") == "tailable":
        return "T1", reasons
    if row.get("data_quality_status") != "trusted":
        return "none", reasons
    if all(_is_gate13(reason) for reason in reasons):
        return "T2", reasons
    return "T3", reasons


def buys_before(data_dir: Path, cutoff: datetime,
                days: int = ACTIVITY_DAYS) -> dict[str, int]:
    """BUY trades per wallet in the ``days`` before ``cutoff`` (archive included)."""
    end = int(cutoff.timestamp())
    start = int((cutoff - timedelta(days=days)).timestamp())
    counts: dict[str, int] = {}
    for line in iter_lines(data_dir / ACTIVITY_FILE):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (not isinstance(row, dict) or row.get("type") != "TRADE"
                or str(row.get("side", "")).upper() != "BUY"):
            continue
        ts = row.get("timestamp")
        if isinstance(ts, int) and not isinstance(ts, bool) and start <= ts < end:
            wallet = str(row.get("proxy_wallet", "")).lower()
            counts[wallet] = counts.get(wallet, 0) + 1
    return counts


def match_comparison(
    t2: Iterable[str], pool: Iterable[str], *, category: dict[str, str],
    buys: dict[str, int],
) -> tuple[dict[str, str], dict[str, int]]:
    """T2 member to matched T3 non-member, and how each pair matched."""
    candidates = sorted(set(pool) - set(t2))
    terciles = _terciles([buys.get(w, 0) for w in sorted(set(pool) | set(t2))])

    def tercile(wallet: str) -> int:
        count = buys.get(wallet, 0)
        return sum(count > edge for edge in terciles)

    pairs: dict[str, str] = {}
    quality = {"category_and_tercile": 0, "tercile_only": 0, "nearest_count": 0,
               "unmatched": 0}
    for member in sorted(t2):
        unused = [w for w in candidates if w not in pairs.values()]
        if not unused:
            quality["unmatched"] += 1
            continue
        both = [w for w in unused if category.get(w) == category.get(member)
                and tercile(w) == tercile(member)]
        same_tercile = [w for w in unused if tercile(w) == tercile(member)]
        pool_now, kind = ((both, "category_and_tercile") if both
                          else (same_tercile, "tercile_only") if same_tercile
                          else (unused, "nearest_count"))
        pairs[member] = min(pool_now, key=lambda w: (abs(buys.get(w, 0)
                                                         - buys.get(member, 0)), w))
        quality[kind] += 1
    return pairs, quality


def generation_at(snapshots: Path, cutoff: datetime) -> dict[str, Any]:
    """The newest score generation that started at or before ``cutoff``, verified.

    A generation started later would carry data the cutoff excludes. Taking the
    newest earlier one, rather than refusing whenever the current one is later,
    means a generation scored between the freeze time and the freeze cycle cannot
    block the freeze. Every retained generation has a manifest written only after
    its outputs were checked, so an unpublished one (its pointer swap interrupted)
    is as complete as a published one.
    """
    started: dict[str, datetime] = {}
    for manifest_path in snapshots.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        when = datetime.fromisoformat(str(manifest["started_at"]))
        if when <= cutoff:
            started[manifest_path.parent.name] = when
    if not started:
        raise ValueError("no score generation started at or before the cutoff")
    run_id = max(started, key=lambda name: (started[name], name))
    return load_generation(snapshots, run_id)


def build(data_dir: Path, *, cutoff: datetime, discovery: dict[str, Any]) -> dict[str, Any]:
    """The frozen configuration at ``cutoff``, from the generation scored before it."""
    snapshots = data_dir / "score-snapshots"
    manifest = generation_at(snapshots, cutoff)
    run_id = manifest["run_id"]
    rows = [json.loads(line) for line in
            (snapshots / run_id / ENRICHMENT).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if manifest.get("files", {}).get(ENRICHMENT, {}).get("score_versions") != ["forecast-v5"]:
        raise ValueError("cohort v1 is defined on a forecast-v5 score generation")
    buys = buys_before(data_dir, cutoff)
    wallets: dict[str, dict[str, Any]] = {}
    for row in rows:
        wallet = str(row.get("proxy_wallet", "")).lower()
        tier, reasons = classify(row)
        wallets[wallet] = {"tier": tier, "reasons": reasons,
                           "clv_only": passes_clv_only(row),
                           "top_category": str(row.get("top_category", "")),
                           "buys_30d": buys.get(wallet, 0),
                           "style_archetype": str(row.get("style_archetype", ""))}
    # Excluded wins over a tier: the cohort stage can exclude a wallet after the score
    # generation that still lists it was written.
    for wallet in sorted(excluded_wallets(data_dir)):
        wallets[wallet] = {"tier": "excluded",
                           "reasons": ["excluded by the cohort stage (systematic)"]}
    for wallet in sorted(_leaderboard_wallets(data_dir) - set(wallets)):
        wallets[wallet] = {"tier": "unscreened",
                           "reasons": ["on the leaderboard, never hydrated (watchlist cap)"]}
    in_tier = {t: sorted(w for w, v in wallets.items()
                         if v["tier"] in _tiers_at_or_above(t))
               for t in ("T1", "T2", "T3")}
    pairs, quality = match_comparison(
        in_tier["T2"], in_tier["T3"],
        category={w: v.get("top_category", "") for w, v in wallets.items()}, buys=buys)
    clv_only = sorted(w for w, v in wallets.items() if v.get("clv_only")
                      and v["tier"] in ("T1", "T2", "T3"))
    config: dict[str, Any] = {
        "cohort_id": COHORT_ID,
        "cutoff": cutoff.astimezone(UTC).isoformat(),
        "discovery": discovery,
        "score_generation": {"run_id": run_id, "started_at": manifest.get("started_at"),
                             "manifest_sha256": hashlib.sha256(
                                 (snapshots / run_id / "manifest.json").read_bytes()
                             ).hexdigest(),
                             "code": manifest.get("code", {}),
                             "score_version": "forecast-v5"},
        "gates": GATES,
        "primary_tier": PRIMARY_TIER,
        "tiers": in_tier,
        "comparison": {"pairs": pairs, "match_quality": quality, "rule": MATCHING},
        "outcome": OUTCOME,
        "cost_model": COST_MODEL,
        "baseline": BASELINE,
        "ablations": {"rules": ABLATIONS, "clv_only": clv_only},
        "window": {"opens": None, "evaluation_date": None, "target_events": 100,
                   "max_days": 42, "set_by": "build step 5, the power count"},
        "screened": dict(sorted(wallets.items())),
    }
    config["config_hash"] = config_hash(config)
    return config


def config_hash(config: dict[str, Any]) -> str:
    body = {key: value for key, value in config.items() if key != "config_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def members(config: dict[str, Any]) -> dict[str, Any]:
    """What the pilot polls every hour: T2, its comparison set (S2) and the CLV-only
    ablation's wallets."""
    t2 = list(config["tiers"]["T2"])
    comparison = sorted(config["comparison"]["pairs"].values())
    clv_only = list(config.get("ablations", {}).get("clv_only", []))
    return {"cohort_id": config["cohort_id"], "config_hash": config["config_hash"],
            "t2": t2, "comparison": comparison, "clv_only": clv_only,
            "wallets": sorted(set(t2) | set(comparison) | set(clv_only))}


def power_window(data_dir: Path, wallets: Iterable[str], cutoff: datetime) -> dict[str, Any]:
    """Step 5 (S6): the window from T2's independent events in the 14 days before the
    cutoff. An event is a distinct event slug a member bought into. Under-powered when
    even the 42-day cap is not expected to reach the target."""
    members_set = {wallet.lower() for wallet in wallets}
    end = int(cutoff.timestamp())
    start = int((cutoff - timedelta(days=POWER_LOOKBACK_DAYS)).timestamp())
    events: set[str] = set()
    for line in iter_lines(data_dir / ACTIVITY_FILE):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (not isinstance(row, dict) or row.get("type") != "TRADE"
                or str(row.get("side", "")).upper() != "BUY"
                or str(row.get("proxy_wallet", "")).lower() not in members_set):
            continue
        ts = row.get("timestamp")
        if isinstance(ts, int) and not isinstance(ts, bool) and start <= ts < end:
            events.add(str(row.get("event_slug", "")) or str(row.get("condition_id", "")))
    per_day = len(events) / POWER_LOOKBACK_DAYS
    needed = math.ceil(TARGET_EVENTS / per_day) if per_day > 0 else None
    days = min(needed, MAX_WINDOW_DAYS) if needed is not None else MAX_WINDOW_DAYS
    return {"opens": cutoff.astimezone(UTC).isoformat(),
            "evaluation_date": (cutoff + timedelta(days=days)).astimezone(UTC).isoformat(),
            "days": days, "events_last_14_days": len(events),
            "events_per_day": round(per_day, 3), "target_events": TARGET_EVENTS,
            "max_days": MAX_WINDOW_DAYS,
            "under_powered": needed is None or needed > MAX_WINDOW_DAYS,
            "set_by": "power count at the freeze (build step 5)"}


def run(data_dir: Path, *, now: datetime, freeze_at: datetime | None,
        discovery: dict[str, Any]) -> dict[str, Any]:
    """Lean-pilot stage.

    - **Frozen:** ``frozen-config.json`` exists. Membership is fixed, and only the
      hash is checked.
    - **Freeze due:** ``freeze_at`` is set and has passed. Build at that cutoff, set
      the window from the power count, write the frozen config once and the member
      list, and log the whole config so it can be committed and its hash recorded.
    - **Otherwise provisional:** rebuild the member list from the current score
      generation, so the hourly polling runs during the burn-in.
    """
    stage = data_dir / STAGE_DIR
    frozen_path = stage / "frozen-config.json"
    if frozen_path.exists():
        config = json.loads(frozen_path.read_text(encoding="utf-8"))
        if config_hash(config) != config.get("config_hash"):
            raise ValueError("frozen cohort config does not match its hash")
        result: dict[str, Any] = {"status": "succeeded", "mode": "frozen",
                                  "config_hash": config["config_hash"],
                                  "members": len(members(config)["wallets"])}
        from . import cohort_v1_eval  # imports this module

        if (cohort_v1_eval.is_due(config, now)
                and not (stage / cohort_v1_eval.RESULT_FILE).exists()):
            # Step 6: the one look, a day after the evaluation date.
            outcome = cohort_v1_eval.evaluate(data_dir, config, now=now)
            cohort_v1_eval.write_once(data_dir, outcome)
            text = json.dumps(outcome, sort_keys=True, separators=(",", ":"))
            for index in range(0, len(text), 8000):
                log.info("cohort v1 result part %d: %s", index // 8000, text[index:index + 8000])
            result |= {"evaluated": True, "verdict": outcome["verdict"]}
        return result
    if freeze_at is not None and now >= freeze_at:
        config = build(data_dir, cutoff=freeze_at, discovery=discovery)
        config["window"] = power_window(data_dir, config["tiers"][PRIMARY_TIER], freeze_at)
        config["config_hash"] = config_hash(config)
        stage.mkdir(parents=True, exist_ok=True)
        with frozen_path.open("x", encoding="utf-8") as out:  # write-once
            out.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        _write_members(stage, members(config) | {"mode": "frozen"})
        text = json.dumps(config, sort_keys=True, separators=(",", ":"))
        for index in range(0, len(text), 8000):  # Railway lines stay readable
            log.info("cohort v1 frozen config part %d: %s", index // 8000, text[index:index + 8000])
        log.info("cohort v1 frozen config_hash=%s", config["config_hash"])
        return {"status": "succeeded", "mode": "frozen_now", "config_hash": config["config_hash"],
                "tiers": {t: len(w) for t, w in config["tiers"].items()},
                "comparison": len(config["comparison"]["pairs"]), "window": config["window"]}
    config = build(data_dir, cutoff=now, discovery=discovery)
    _write_members(stage, members(config) | {"mode": "provisional",
                                             "built_at": now.astimezone(UTC).isoformat()})
    return {"status": "succeeded", "mode": "provisional",
            "tiers": {t: len(w) for t, w in config["tiers"].items()},
            "comparison": len(config["comparison"]["pairs"]),
            "members": len(members(config)["wallets"])}


def member_wallets(data_dir: Path) -> frozenset[str]:
    """The wallets the pilot polls every run: frozen or provisional, else none."""
    path = data_dir / STAGE_DIR / "members.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    wallets = data.get("wallets") if isinstance(data, dict) else None
    return frozenset(str(w).lower() for w in wallets) if isinstance(wallets, list) else frozenset()


def frozen_members(data_dir: Path) -> frozenset[str]:
    """Members protected from exclusion: only once the config is frozen (S2)."""
    path = data_dir / STAGE_DIR / "frozen-config.json"
    if not path.exists():
        return frozenset()
    return frozenset(members(json.loads(path.read_text(encoding="utf-8")))["wallets"])


def _write_members(stage: Path, roster: dict[str, Any]) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / "members.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roster, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _is_gate13(reason: str) -> bool:
    return "closing-line" in reason or "post-entry CLV" in reason


def _tiers_at_or_above(tier: str) -> frozenset[str]:
    return {"T1": frozenset({"T1"}), "T2": frozenset({"T1", "T2"}),
            "T3": frozenset({"T1", "T2", "T3"})}[tier]


def _terciles(values: list[int]) -> tuple[int, int]:
    if not values:
        return (0, 0)
    ordered = sorted(values)
    return (ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3])


def _leaderboard_wallets(data_dir: Path) -> set[str]:
    found: set[str] = set()
    for line in iter_lines(data_dir / LEADERBOARD_FILE):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("proxy_wallet"):
            found.add(str(row["proxy_wallet"]).lower())
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--pilot-config", type=Path, required=True,
                        help="deploy/lean-pilot.json: records the discovery source")
    parser.add_argument("--cutoff", required=True, help="ISO-8601 with a timezone")
    parser.add_argument("--out", type=Path, required=True, help="frozen-config.json")
    parser.add_argument("--members-out", type=Path, help="the pilot's member list")
    args = parser.parse_args(argv)
    cutoff = datetime.fromisoformat(args.cutoff)
    if cutoff.tzinfo is None:
        raise SystemExit("--cutoff must include a timezone")
    pilot = json.loads(args.pilot_config.read_text(encoding="utf-8"))
    discovery = {key: pilot.get(key) for key in (
        "leaderboard_metric", "leaderboard_window", "leaderboard_limit",
        "max_watchlist_wallets")}
    config = build(args.data_dir, cutoff=cutoff, discovery=discovery)
    args.out.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.members_out:
        args.members_out.write_text(json.dumps(members(config), indent=2) + "\n",
                                    encoding="utf-8")
    log.info("cohort v1: T1 %d, T2 %d, T3 %d, comparison %d, config %s",
             len(config["tiers"]["T1"]), len(config["tiers"]["T2"]),
             len(config["tiers"]["T3"]), len(config["comparison"]["pairs"]),
             config["config_hash"][:12])
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
