"""Bounded offline metadata-cause audit of a verified enrichment shadow snapshot.

The qualification replay changes only metadata reasons, using the original
model outputs. It neither refits the model nor publishes production scores.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .activity_parquet import BUCKETS, connect, fingerprint, load_manifest
from .gate_attrition import DATA_REASONS, analyze
from .gate_attrition import build_report as verify_scores
from .metadata_coverage import MetadataCoverage
from .runner import _iter_jsonl, _load_market_index

log = logging.getLogger(__name__)
METADATA_REASON = "incomplete market metadata"
QUERY = """
SELECT DISTINCT wallet, json_extract_string(raw_json, '$.condition_id') AS condition_id
FROM read_parquet(?, hive_partitioning=true)
WHERE wallet_bucket=? AND json_extract_string(raw_json, '$.type')='TRADE'
  AND condition_id IS NOT NULL AND condition_id<>''
"""


def replay_metadata_gate(row: dict[str, Any], coverage: MetadataCoverage) -> dict[str, Any]:
    """Replay this one gate without guessing unrounded scores or altering other reasons."""
    data = [r for r in row["data_quality_reasons"] if r != METADATA_REASON]
    model = [r for r in row["tailability_reasons"] if r not in DATA_REASONS]
    if coverage.ratio < 1:
        data.append(METADATA_REASON)
    reasons = data + model
    return {**row, "data_quality_reasons": data, "data_quality_status": "untrusted" if data else "trusted",
            "tailability_reasons": reasons, "tailability_status": "blocked" if reasons else "tailable"}


def _verify_fingerprint(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    actual = fingerprint(path)
    if any(actual[field] != expected.get(field) for field in ("bytes", "sha256")):
        raise ValueError(f"Frozen source does not match benchmark: {path.name}")
    return actual


def build_audit(snapshot: Path, benchmark: Path, data_dir: Path, dataset: Path,
                *, memory_mb: int = 512) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    verified = verify_scores(snapshot, benchmark)
    evidence = json.loads(benchmark.read_text(encoding="utf-8"))
    markets = data_dir / "polymarket_markets.jsonl"
    hydration_path = data_dir / "polymarket_wallet_hydration.jsonl"
    signatures = {
        "markets": _verify_fingerprint(markets, evidence["inputs"]["markets"]),
        "hydration": _verify_fingerprint(hydration_path, evidence["inputs"]["hydration"]),
    }
    manifest = load_manifest(dataset)
    if manifest["source_fingerprint"]["sha256"] != evidence["inputs"]["activity"]["sha256"]:
        raise ValueError("Parquet activity source differs from enrichment benchmark")
    expected_files = evidence["dataset"]["files"]
    actual_files = {"manifest.json", *[item["path"] for item in manifest["files"]]}
    if set(expected_files) != actual_files:
        raise ValueError("Parquet file inventory differs from benchmark")
    dataset_root = dataset.resolve()
    for name, expected in expected_files.items():
        path = (dataset_root / name).resolve()
        if not path.is_relative_to(dataset_root):
            raise ValueError("Invalid dataset path")
        _verify_fingerprint(path, expected)

    rows = list(_iter_jsonl(snapshot))
    if len(rows) != verified["analysis"]["wallets"]:
        raise ValueError("Score snapshot changed before audit")
    score_wallets = {r["proxy_wallet"].lower() for r in rows}
    candidates = {r["proxy_wallet"].lower() for r in rows if r["tailability_reasons"] == [METADATA_REASON]}
    hydration = {row["proxy_wallet"].lower(): row for row in _iter_jsonl(hydration_path)}
    market_index = _load_market_index(markets)
    # Materialize only distinct pairs in one bucket at a time. Neither all raw
    # activity nor the collector's deduplication index is loaded into Python.
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    missing_candidates: dict[str, list[str]] = defaultdict(list)
    glob = str(dataset / "activity" / "**" / "*.parquet")
    with connect(memory_mb=memory_mb, threads=1) as db:
        for bucket in range(BUCKETS):
            db.execute(QUERY, [glob, bucket])
            while batch := db.fetchmany(4096):
                for wallet, condition in batch:
                    if wallet not in score_wallets:
                        raise ValueError("Activity wallet absent from frozen scores")
                    record = market_index.get(condition)
                    reason = ("missing_market_rows" if record is None else
                              "covered" if record["has_settlement"] else "present_unsettled")
                    counts[wallet][reason] += 1
                    if reason == "missing_market_rows" and wallet in candidates:
                        missing_candidates[wallet].append(condition)
            if bucket % 8 == 0:
                log.info("metadata audit bucket %d/%d", bucket + 1, BUCKETS)
    replays = []
    wallet_results = []
    mismatched = 0
    for row in rows:
        wallet = row["proxy_wallet"].lower()
        count = counts[wallet]
        coverage = MetadataCoverage(sum(count.values()), count["covered"],
                                    count["missing_market_rows"], count["present_unsettled"])
        saved = hydration.get(wallet)
        if saved is None:
            # Missing hydration must retain all original unknown trust flags.
            replay = dict(row)
            stored = None
        else:
            replay = replay_metadata_gate(row, coverage)
            stored = {key: saved[key] for key in (
                "metadata_condition_count", "metadata_covered_count", "metadata_coverage")}
            if (stored["metadata_condition_count"], stored["metadata_covered_count"],
                stored["metadata_coverage"]) != (coverage.conditions, coverage.covered, coverage.ratio):
                mismatched += 1
        replays.append(replay)
        wallet_results.append({
            "wallet": wallet, "metadata_only_candidate": wallet in candidates,
            "stored": stored, "recomputed": {**asdict(coverage), "ratio": coverage.ratio},
            "missing_condition_ids": sorted(missing_candidates[wallet]) if wallet in candidates else [],
        })
    after = analyze(replays)
    totals = sum(counts.values(), Counter())
    # Retain explicit lineage without publishing the user's absolute filesystem paths.
    report = {
        "schema_version": 1, "status": "passed", "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Frozen historical scores and market inputs; no new observations or model refit",
        "coverage_rule": "Existing forecast-v4: any stored closed row or outcome price >=0.99",
        "provenance": {**verified["provenance"], "verified_context": signatures,
                       "verified_parquet_files": len(expected_files),
                       "audit_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "settings": {"duckdb_memory_mb": memory_mb, "threads": 1, "wallet_buckets": BUCKETS},
        "query": QUERY.strip(),
        "summary": {
            "wallets": len(rows), "hydration_count_mismatches": mismatched,
            "wallet_condition_pairs": sum(totals.values()), "condition_reason_counts": dict(totals),
            "metadata_failures_before": verified["analysis"]["waterfall"][5]["isolated_failures"],
            "metadata_failures_after": after["waterfall"][5]["isolated_failures"],
            "trusted_before": verified["analysis"]["trusted_wallets"],
            "trusted_after": after["trusted_wallets"],
            "tailable_before": verified["analysis"]["tailable_wallets"],
            "tailable_after": after["tailable_wallets"],
        },
        "before_waterfall": verified["analysis"]["waterfall"],
        "after_waterfall": after["waterfall"],
        "after_counterfactuals": after["counterfactuals"],
        "wallets": sorted(wallet_results, key=lambda item: item["wallet"]),
        "limitations": [
            "Counts are distinct wallet-condition pairs, not unique global markets or trade rows.",
            ("Present unsettled means an existing market fails the legacy coverage rule; "
            "it does not establish missing metadata or an invalid market."),
            ("Absent records have unknown historical fetch cause. These inputs cannot distinguish "
            "never requested, empty Gamma response, HTTP error, or a capped/interrupted backfill."),
            "Category absence and positions pagination are not inputs to this coverage rule.",
            ("The replay changes only metadata reasons; no numeric score, economic field, "
            "other rejection rule, or scoring threshold was changed."),
            "No currently qualified wallet or profitable follower strategy is established.",
        ],
    }
    for key, path in (("markets", markets), ("hydration", hydration_path)):
        _verify_fingerprint(path, signatures[key])
    for name, expected in expected_files.items():
        _verify_fingerprint(dataset / name, expected)
    for path, expected in ((snapshot, verified["provenance"]["snapshot"]),
                           (benchmark, {"bytes": len(benchmark.read_bytes()),
                                        "sha256": verified["provenance"]["shadow_benchmark"]["sha256"]})):
        _verify_fingerprint(path, expected)
    return report, replays


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = ["# Stage 1: metadata coverage diagnosis", "",
             f"Generated: {report['generated_at']}. {report['scope']}.", "",
             "## Same-input coverage repair", "",
             "| Measure | Stored | Recomputed |", "|---|---:|---:|",
             f"| Metadata failures | {s['metadata_failures_before']} | {s['metadata_failures_after']} |",
             f"| Data-trusted wallets | {s['trusted_before']} | {s['trusted_after']} |",
             f"| Tailable wallets | {s['tailable_before']} | {s['tailable_after']} |", "",
             (f"{s['hydration_count_mismatches']} wallets have saved coverage counts/ratios that "
             "disagree with the scoring snapshot. All other qualification decisions were held fixed."), "",
             "## Coverage decomposition", "", "| Wallet-condition pair status | Count |", "|---|---:|"]
    lines.extend(f"| {reason} | {count} |" for reason, count in s["condition_reason_counts"].items())
    lines += ["", "## The two metadata-only candidates", "",
              "| Wallet | Conditions | Stored covered | Recomputed covered | Present unsettled | Missing rows |",
              "|---|---:|---:|---:|---:|---:|"]
    for wallet in report["wallets"]:
        if wallet["metadata_only_candidate"]:
            c = wallet["recomputed"]
            lines.append(f"| `{wallet['wallet']}` | {c['conditions']} | "
                         f"{wallet['stored']['metadata_covered_count']} | {c['covered']} | "
                         f"{c['present_unsettled']} | {c['missing_market_rows']} |")
    lines += ["", "## Limits", "", *[f"- {item}" for item in report["limitations"]], "",
              ("The JSON includes per-wallet counts, candidate missing condition IDs, full before/after "
              "waterfalls, and counterfactuals. Source and Parquet hashes were checked at both ends."), ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New diagnostic directory")
    parser.add_argument("--memory-mb", type=int, default=512)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be a new directory")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report, replays = build_audit(args.snapshot, args.benchmark, args.data_dir, args.dataset,
                                  memory_mb=args.memory_mb)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                           encoding="utf-8", newline="\n")
    (args.output / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    with (args.output / "diagnostic-replay.jsonl").open("x", encoding="utf-8", newline="\n") as handle:
        for row in replays:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
