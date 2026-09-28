"""Read-only forecast-v4 gate diagnostics over a verified enrichment snapshot.

No collection, scoring, threshold changes, or production-store imports. Persisted
reasons preserve pre-rounding decisions; rounded metrics describe margins only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Gate:
    number: int
    label: str
    reasons: tuple[str, ...]


GATES = (
    Gate(1, "Complete activity history", ("incomplete activity history",)),
    Gate(2, "Complete current positions", ("incomplete current positions",)),
    Gate(3, "Complete closed positions", ("incomplete closed positions",)),
    Gate(4, "Complete all-time economics", ("missing all-time economics",)),
    Gate(5, "Complete 30-day economics", ("missing 30-day economics",)),
    Gate(6, "Complete market metadata", ("incomplete market metadata",)),
    Gate(7, "Independent settled events >=20", ("fewer than 20 independent settled events",)),
    Gate(8, "Forecast confidence >=80%", ("forecast confidence below 80%",)),
    Gate(9, "Conservative forecast edge >0", ("conservative edge is not positive",)),
    Gate(10, "Positive all-time PnL/ROI and nonnegative 30-day PnL", (
        "negative or zero all-time PnL", "negative or zero all-time ROI", "recent PnL below zero",
    )),
    Gate(11, "Recent independent events >=5", (
        "fewer than 5 recent independent settled events",
    )),
    Gate(12, "Recent forecast edge >=0 when gate 11 passes", (
        "recent forecast edge is negative",
    )),
    Gate(13, "CLV sample >=10 and lower bound >0", (
        "fewer than 10 closing-line observations", "closing-line value not confidently positive",
    )),
)
KNOWN_REASONS = {reason for gate in GATES for reason in gate.reasons}
DATA_REASONS = {reason for gate in GATES[:6] for reason in gate.reasons}
# Serialization precision in forecast-v4. Intervals conservatively include ties.
PRECISION = {
    "independent_settled_events": 4, "forecast_skill_likelihood": 6,
    "forecast_edge_lower_bound": 6, "all_time_pnl_usdc": 2,
    "all_time_roi": 8, "pnl_30d_usdc": 2, "recent_independent_events": 4,
    "recent_edge_mean": 6, "clv_sample_size": 4, "clv_lower_bound": 6,
}
LIMITATIONS = [
    "Historical, selected wallet cohort; this is not a live feed or a representative census.",
    ("Suspending data gates does not repair missing inputs or refit scores; it is not a causal "
    "estimate of what complete data would do."),
    ("Baseline gates use stored pre-rounding rejection reasons. Gate 12 is not evaluated when "
    "gate 11 fails. Gate 13's confidence test is conditional on sufficient CLV sample."),
    ("Margins use rounded serialized metrics. The CLV-5 counterfactual reports a range if "
    "rounding prevents an exact decision; its positive lower-bound requirement is retained."),
    ("Forecast posterior confidence is model-dependent, not a calibrated population-wide "
    "false-discovery guarantee. No prospective, fee-adjusted copying profit is established."),
    ("Raw activity and market files were hashed in the linked shadow benchmark, not rehashed "
    "by this diagnostic. This run verifies the derived score file and benchmark receipt."),
]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> bytes:
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Diagnostic input exceeds 128 MiB; use a bounded frozen score snapshot")
    with path.open("rb") as handle:
        raw = handle.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("Diagnostic input exceeds 128 MiB")
    return raw


def _reasons(row: dict[str, Any], key: str) -> set[str]:
    value = row.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Missing or invalid {key}")
    if len(set(value)) != len(value):
        raise ValueError(f"Duplicate {key}")
    return set(value)


def _at_least(row: dict[str, Any], field: str, threshold: float,
              *, strict: bool = False) -> bool | None:
    """A decision only when the entire serialization interval agrees."""
    value = float(row[field])
    half_unit = 0.5 * 10 ** -PRECISION[field]
    low, high = value - half_unit, value + half_unit
    if low > threshold if strict else low >= threshold:
        return True
    if high <= threshold if strict else high < threshold:
        return False
    return None


def _all(values: list[bool | None]) -> bool | None:
    if False in values:
        return False
    return None if None in values else True


def _check_decision(failed: bool, decision: bool | None, field: str) -> None:
    if decision is not None and failed == decision:
        raise ValueError(f"Stored gate reasons contradict rounded metric: {field}")


def _validate(row: dict[str, Any]) -> set[int]:
    wallet = row.get("proxy_wallet")
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise ValueError("Invalid wallet key")
    if row.get("score_version") != "forecast-v4":
        raise ValueError("Only explicit forecast-v4 snapshots are supported")
    reasons = _reasons(row, "tailability_reasons")
    data = _reasons(row, "data_quality_reasons")
    if reasons - KNOWN_REASONS or data - DATA_REASONS:
        raise ValueError("Unknown gate reason; update the versioned diagnostic before proceeding")
    if data != reasons & DATA_REASONS:
        raise ValueError("Data reasons disagree with tailability reasons")
    if row.get("data_quality_status") != ("untrusted" if data else "trusted"):
        raise ValueError("Data status disagrees with reasons")
    if row.get("tailability_status") != ("blocked" if reasons else "tailable"):
        raise ValueError("Tailability status disagrees with reasons")
    failed = {gate.number for gate in GATES if reasons.intersection(gate.reasons)}
    if type(row.get("economic_qualified")) is not bool or row["economic_qualified"] != (
        10 not in failed
    ):
        raise ValueError("Economic status disagrees with reasons")
    for field in PRECISION:
        value = row.get(field)
        if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"Missing or nonfinite metric: {field}")
        if ("events" in field or field == "clv_sample_size") and value < 0:
            raise ValueError(f"Negative sample size: {field}")
    if not 0 <= row["forecast_skill_likelihood"] <= 1:
        raise ValueError("Forecast confidence must be in [0,1]")
    for number, field, threshold, strict in (
        (7, "independent_settled_events", 20, False),
        (8, "forecast_skill_likelihood", .8, False),
        (9, "forecast_edge_lower_bound", 0, True),
        (11, "recent_independent_events", 5, False),
    ):
        _check_decision(number in failed, _at_least(row, field, threshold, strict=strict), field)
    for reason, field, strict in (
        ("negative or zero all-time PnL", "all_time_pnl_usdc", True),
        ("negative or zero all-time ROI", "all_time_roi", True),
        ("recent PnL below zero", "pnl_30d_usdc", False),
    ):
        _check_decision(reason in reasons, _at_least(row, field, 0, strict=strict), field)
    if 11 in failed:
        if 12 in failed:
            raise ValueError("Gate 12 cannot be evaluated after gate 11 fails")
    else:
        _check_decision(12 in failed, _at_least(row, "recent_edge_mean", 0), "recent_edge_mean")
    low_sample = "fewer than 10 closing-line observations" in reasons
    bad_clv = "closing-line value not confidently positive" in reasons
    _check_decision(low_sample, _at_least(row, "clv_sample_size", 10), "clv_sample_size")
    if low_sample and bad_clv:
        raise ValueError("CLV confidence is conditional on adequate sample")
    if not low_sample:
        _check_decision(bad_clv, _at_least(row, "clv_lower_bound", 0, strict=True), "clv_lower_bound")
    return failed


def _clv_five(row: dict[str, Any], failed: set[int]) -> bool | None:
    if 13 not in failed:
        return True  # Exact persisted sample >=10 already implies >=5.
    if "closing-line value not confidently positive" in row["tailability_reasons"]:
        return False  # Exact persisted confidence failure remains a failure.
    return _all([_at_least(row, "clv_sample_size", 5),
                 _at_least(row, "clv_lower_bound", 0, strict=True)])


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    values = sorted(values)
    if not values:
        return {"n": 0, "min": None, "p10": None, "median": None, "p90": None, "max": None}

    def quantile(p: float) -> float:
        index = (len(values) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 8)

    return {"n": len(values), "min": values[0], "p10": quantile(.1), "median": quantile(.5),
            "p90": quantile(.9), "max": values[-1]}


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("Snapshot has no wallets")
    failed = [_validate(row) for row in rows]
    if len({row["proxy_wallet"].lower() for row in rows}) != len(rows):
        raise ValueError("Duplicate wallet keys; one score per wallet is required")
    survivors = set(range(len(rows)))
    waterfall = []
    for gate in GATES:
        before = len(survivors)
        survivors = {i for i in survivors if gate.number not in failed[i]}
        skipped = sum(11 in f for f in failed) if gate.number == 12 else 0
        waterfall.append({
            "gate": gate.number, "label": gate.label, "entering": before,
            "removed": before - len(survivors), "remaining": len(survivors),
            "isolated_failures": sum(gate.number in f for f in failed),
            "not_evaluated": skipped,
            "only_failure": sum(f == {gate.number} for f in failed),
        })
    lower = upper = 0
    ambiguous = 0
    for row, f in zip(rows, failed, strict=True):
        if f - {13}:
            continue
        passes = _clv_five(row, f)
        lower += passes is True
        upper += passes is not False
        ambiguous += passes is None
    counterfactuals = {
        "data_gates_suspended": sum(not (f - set(range(1, 7))) for f in failed),
        "metadata_gate_suspended": sum(not (f - {6}) for f in failed),
        "clv_minimum_five": {"qualified_min": lower, "qualified_max": upper,
                             "ambiguous_eligible_wallets": ambiguous},
    }
    coverage_only = sum(bool(f) and not (f - set(range(1, 7))) for f in failed)
    performance_only = sum(bool(f) and min(f) > 6 for f in failed)
    both = sum(any(g <= 6 for g in f) and any(g > 6 for g in f) for f in failed)
    histogram = Counter(sum(g > 6 for g in f) for f in failed)
    patterns = Counter(tuple(sorted(f)) for f in failed)
    return {
        "wallets": len(rows), "tailable_wallets": len(survivors),
        "trusted_wallets": sum(not any(g <= 6 for g in f) for f in failed),
        "waterfall": waterfall, "counterfactuals": counterfactuals,
        "failure_groups": {"coverage_only": coverage_only,
                           "model_or_economics_only": performance_only,
                           "both": both, "neither": len(survivors)},
        "model_economic_failure_histogram": {str(k): histogram[k] for k in sorted(histogram)},
        "failure_patterns": [{"gates": list(gates), "wallets": n}
                             for gates, n in sorted(patterns.items(), key=lambda x: (-x[1], x[0]))],
        "pairwise_failures": [
            {"first": a, "second": b, "wallets": sum(a in f and b in f for f in failed)}
            for a in range(1, 14) for b in range(a + 1, 14)
        ],
        "rounded_metric_distributions": {
            scope: {field: _distribution([float(row[field]) for row, f in zip(rows, failed, strict=True)
                                         if scope == "all_wallets" or not any(g <= 6 for g in f)])
                    for field in PRECISION}
            for scope in ("all_wallets", "trusted_wallets")
        },
        "reasons": dict(sorted(Counter(reason for row in rows
                                       for reason in row["tailability_reasons"]).items())),
        "checks": {"unique_wallet_keys": True, "known_score_version_and_reasons": True,
                   "stored_status_and_reason_parity": True,
                   "rounded_metric_intervals_compatible_with_reasons": True},
    }


def build_report(snapshot: Path, benchmark: Path) -> dict[str, Any]:
    raw, receipt_raw = _read(snapshot), _read(benchmark)
    receipt = json.loads(receipt_raw)
    if not isinstance(receipt, dict) or receipt.get("status") != "passed":
        raise ValueError("A passed enrichment shadow benchmark is required")
    records = receipt.get("measurements", [])
    if not isinstance(records, list):
        raise ValueError("Invalid shadow measurements")
    engines = {item.get("operation"): item for item in records if isinstance(item, dict)}
    for engine in ("jsonl", "parquet"):
        result = engines.get(engine, {}).get("result", {})
        expected = result.get("outputs", {}).get("enrichments", {})
        if expected.get("sha256") != _digest(raw) or expected.get("bytes") != len(raw):
            raise ValueError(f"Score snapshot hash/size does not match {engine} shadow evidence")
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            raise ValueError("Blank score record")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Score record must be an object")
        rows.append(value)
    analysis = analyze(rows)
    for engine in ("jsonl", "parquet"):
        result = engines[engine]["result"]
        if (result.get("wallets"), result.get("tailable_wallets")) != (
            analysis["wallets"], analysis["tailable_wallets"]
        ):
            raise ValueError("Wallet counts disagree with shadow benchmark")
    if _digest(_read(snapshot)) != _digest(raw) or _digest(_read(benchmark)) != _digest(receipt_raw):
        raise ValueError("Inputs changed during analysis")
    return {
        "schema_version": 1, "status": "passed", "score_version": "forecast-v4",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "All wallets in the frozen enrichment shadow output; diagnostic only",
        "provenance": {
            "snapshot": {"name": snapshot.name, "bytes": len(raw), "sha256": _digest(raw)},
            "shadow_benchmark": {"name": benchmark.name, "sha256": _digest(receipt_raw)},
            "scoring_code": receipt.get("code"),
            "upstream_fingerprints_from_benchmark": receipt.get("inputs"),
            "diagnostic_source_sha256": _digest(Path(__file__).read_bytes()),
        },
        "analysis": analysis, "limitations": LIMITATIONS,
    }


def render_markdown(report: dict[str, Any]) -> str:
    analysis = report["analysis"]
    cf = analysis["counterfactuals"]
    lines = [
        "# Stage 0: wallet qualification attrition", "",
        f"Generated: {report['generated_at']}. Score version: `{report['score_version']}`.", "",
        f"Frozen snapshot SHA-256: `{report['provenance']['snapshot']['sha256']}`.", "",
        (f"{analysis['wallets']} wallets; {analysis['trusted_wallets']} data-trusted; "
        f"{analysis['tailable_wallets']} tailable. No production settings changed."), "",
        "## Cumulative waterfall and isolated failures", "",
        ("The cumulative columns apply blueprint gates 1–13 in order. Isolated counts use "
        "each stored gate decision across the full cohort, so they overlap. Gate 12 is "
        "conditionally evaluated; skipped wallets are not evidence of a passing recent edge."), "",
        "| Gate | Requirement | Entering | Removed | Remaining | Isolated failures | Not evaluated | Only failure |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for g in analysis["waterfall"]:
        lines.append(f"| {g['gate']} | {g['label']} | {g['entering']} | {g['removed']} | "
                     f"{g['remaining']} | {g['isolated_failures']} | {g['not_evaluated']} | "
                     f"{g['only_failure']} |")
    lines += ["", "## Diagnostic counterfactuals", "",
              f"- Suspend data gates 1–6: **{cf['data_gates_suspended']}** qualify.",
              f"- Suspend metadata gate 6 only: **{cf['metadata_gate_suspended']}** qualify.",
              ("- Lower CLV sample from 10 to 5, keep positive lower bound and all other gates: "
              f"**{cf['clv_minimum_five']['qualified_min']}–"
              f"{cf['clv_minimum_five']['qualified_max']}** qualify "
              f"({cf['clv_minimum_five']['ambiguous_eligible_wallets']} ambiguous at rounding boundaries)."),
              "", "## Failure overlap", "", "| Group | Wallets |", "|---|---:|"]
    for group, n in analysis["failure_groups"].items():
        lines.append(f"| {group.replace('_', ' ')} | {n} |")
    lines += ["", "## Rounded score margins", "",
              "These are descriptive percentiles, not uncertainty intervals or causal effects.", "",
              "| Metric | Cohort | p10 | Median | p90 |", "|---|---|---:|---:|---:|"]
    for scope, fields in analysis["rounded_metric_distributions"].items():
        for field, values in fields.items():
            lines.append(f"| {field} | {scope} | {values['p10']} | {values['median']} | {values['p90']} |")
    lines += ["", "## Scope and verification", "", *[f"- {s}" for s in report["limitations"]],
              "", ("Input hashes match both JSONL and Parquet shadow outputs. Wallet keys, score "
              "version, reasons, statuses, and compatibility with rounded metric intervals "
              "were checked. Pairwise overlaps and full failure patterns are in the companion JSON."), ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New .json report path; also writes .md")
    args = parser.parse_args()
    json_path = args.output
    md_path = json_path.with_suffix(".md")
    if json_path.suffix != ".json" or json_path.exists() or md_path.exists():
        parser.error("Output must be a new .json path with no existing .md companion")
    report = build_report(args.snapshot, args.benchmark)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with md_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(render_markdown(report))


if __name__ == "__main__":
    main()
