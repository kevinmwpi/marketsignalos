"""Compare full two-pass scoring from JSONL and Parquet without production writes.

Requires the benchmark extra. This validates storage equivalence on a frozen
snapshot; it is neither a point-in-time backtest nor evidence of trading alpha.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .activity_parquet import BUCKETS, fingerprint, load_manifest
from .activity_reader import ParquetActivityReader
from .models import PolymarketActivity, PolymarketWalletBet, PolymarketWalletEnrichment
from .runner import (
    _iter_activity_shards,
    _load_leaderboard_records,
    _load_market_records,
    _load_price_snapshot_records,
    _shard_activity_by_wallet,
)
from .skill_computation import compute_enrichment_outputs_streaming
from .storage import JsonlWalletHydrationStore
from .storage_benchmark import measured_job

log = logging.getLogger(__name__)
INPUTS = {
    "activity": "polymarket_activity.jsonl",
    "markets": "polymarket_markets.jsonl",
    "leaderboard": "polymarket_leaderboard.jsonl",
    "hydration": "polymarket_wallet_hydration.jsonl",
    "prices": "polymarket_price_snapshots.jsonl",
}


def canonical_record(record: PolymarketWalletBet | PolymarketWalletEnrichment) -> str:
    row = asdict(record)
    # Only the output's wall-clock stamp is excluded. Source observations,
    # rankings, nested drivers, flags, and every floating value remain exact.
    del row["computed_at"]
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def score(job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    output = Path(job["output"])
    output.mkdir()  # Never overwrite another worker's output.
    inputs = {name: Path(path) for name, path in job["inputs"].items()}
    markets = _load_market_records(inputs["markets"])
    leaderboard = _load_leaderboard_records(inputs["leaderboard"])
    hydration = JsonlWalletHydrationStore(inputs["hydration"]).load_hydration()
    prices = _load_price_snapshot_records(inputs["prices"])
    log.info("loaded scoring context markets=%d leaderboard=%d hydration=%d prices=%d",
             len(markets), len(leaderboard), len(hydration), len(prices))
    factory: Callable[[], Iterator[list[PolymarketActivity]]]
    preparation_started = time.perf_counter()
    if job["operation"] == "jsonl":
        paths = _shard_activity_by_wallet(inputs["activity"], shard_count=BUCKETS,
                                          tmp_dir=output / "shards")
        factory = lambda: _iter_activity_shards(paths)
    elif job["operation"] == "parquet":
        factory = ParquetActivityReader(Path(job["dataset"]), memory_mb=job["memory_mb"],
                                       threads=job["threads"])
    else:
        raise ValueError("Unknown scoring source")
    preparation_seconds = time.perf_counter() - preparation_started
    rows_per_pass: list[int] = []

    def counted() -> Iterator[list[PolymarketActivity]]:
        count = 0
        for shard in factory():
            count += len(shard)
            yield shard
        rows_per_pass.append(count)

    bets = 0
    scoring_started = time.perf_counter()
    with (output / "bets.jsonl").open("w", encoding="utf-8", newline="\n") as handle:

        def sink(batch: list[PolymarketWalletBet]) -> None:
            nonlocal bets
            for record in batch:
                handle.write(canonical_record(record))
            bets += len(batch)

        enrichments, unused = compute_enrichment_outputs_streaming(
            activity_shards_factory=counted, markets=markets, leaderboard=leaderboard,
            hydration_by_wallet=hydration, price_snapshots=prices, bet_sink=sink,
        )
    assert not unused
    with (output / "enrichments.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for enrichment in enrichments:
            handle.write(canonical_record(enrichment))
    scoring_seconds = time.perf_counter() - scoring_started
    if len(rows_per_pass) != 2 or rows_per_pass[0] != rows_per_pass[1]:
        raise RuntimeError("Scorer did not replay the same number of rows twice")
    return {
        "elapsed_seconds": time.perf_counter() - started,
        "os_peak_rss_bytes": getattr(psutil.Process().memory_info(), "peak_wset", None),
        "result": {
            "rows_per_pass": rows_per_pass, "wallets": len(enrichments), "bets": bets,
            "resolved_bets": sum(e.resolved_trades for e in enrichments),
            "tailable_wallets": sum(e.tailability_status == "tailable" for e in enrichments),
            "preparation_seconds": preparation_seconds, "scoring_seconds": scoring_seconds,
            "outputs": {name: fingerprint(output / f"{name}.jsonl")
                        for name in ("bets", "enrichments")},
        },
    }


def compare_outputs(left: Path, right: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("enrichments", "bets"):
        a, b = left / f"{name}.jsonl", right / f"{name}.jsonl"
        sig_a, sig_b = fingerprint(a), fingerprint(b)
        if (sig_a["bytes"], sig_a["sha256"]) != (sig_b["bytes"], sig_b["sha256"]):
            with a.open(encoding="utf-8") as x, b.open(encoding="utf-8") as y:
                line = 0
                while True:
                    line += 1
                    one, two = x.readline(), y.readline()
                    if one != two:
                        raise RuntimeError(f"{name} differ at canonical output line {line}")
                    if not one:
                        break
            raise RuntimeError(f"{name} hash mismatch")
        result[name] = {"bytes": sig_a["bytes"], "sha256": sig_a["sha256"]}
    return result


def _dataset_fingerprints(dataset: Path) -> dict[str, Any]:
    manifest = load_manifest(dataset)
    return {name: fingerprint(dataset / name)
            for name in ["manifest.json", *[entry["path"] for entry in manifest["files"]]]}


def _code_identity() -> dict[str, str]:
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for source in sorted(package.glob("*.py")):
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=package,
                                         text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    return {"git_head": commit, "package_source_sha256": digest.hexdigest()}


def run_shadow(data_dir: Path, dataset: Path, output: Path, *, memory_mb: int = 512,
               threads: int = 2, rss_limit_mb: int = 4096,
               timeout_seconds: int = 3600) -> dict[str, Any]:
    data_dir, dataset, output = data_dir.resolve(strict=True), dataset.resolve(strict=True), output.resolve()
    if output.is_relative_to(data_dir) or output.is_relative_to(dataset):
        raise ValueError("Shadow output must be outside the source data and dataset")
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "status": "running", "started_at": datetime.now(UTC).isoformat(),
        "code": _code_identity(), "scope": "all snapshot wallets; full two-pass enrichment",
        "comparison": "Exact canonical JSON bytes in emitted order; exclude computed_at only",
        "settings": {"memory_mb": memory_mb, "threads": threads,
                     "rss_limit_mb": rss_limit_mb, "timeout_seconds": timeout_seconds},
        "measurements": [],
    }
    try:
        manifest = load_manifest(dataset)
        originals = {name: data_dir / file for name, file in INPUTS.items()}
        signatures = {name: fingerprint(path) for name, path in originals.items()}
        if signatures["activity"] != manifest["source_fingerprint"]:
            raise ValueError("Activity source does not match the Parquet snapshot fingerprint")
        parquet_signatures = _dataset_fingerprints(dataset)
        report["inputs"] = signatures
        report["dataset"] = {"rows": manifest["rows"], "wallets": manifest["wallets"],
                             "files": parquet_signatures}
        inputs = dict(originals)
        # Ancillary snapshots are small enough to freeze locally. Activity stays
        # in place, with full SHA-256 checks at both ends of the experiment.
        (output / "inputs").mkdir()
        for name, source in originals.items():
            if name == "activity":
                continue
            inputs[name] = output / "inputs" / source.name
            shutil.copy2(source, inputs[name])
            if fingerprint(inputs[name]) != signatures[name]:
                raise RuntimeError(f"Input changed while freezing {name}")
        job = {"inputs": {name: str(path) for name, path in inputs.items()},
               "dataset": str(dataset), "memory_mb": memory_mb, "threads": threads}
        for operation in ("jsonl", "parquet"):
            measurement = measured_job(
                {**job, "operation": operation, "output": str(output / operation)},
                output, operation, rss_limit_mb=rss_limit_mb, timeout_seconds=timeout_seconds,
                worker_module="marketsignalos_polymarket.enrichment_shadow",
            )
            report["measurements"].append(measurement)
            # Remove only the known scratch shard directory made by this worker.
            # Never remove input data or reuse another run's directory.
            if operation == "jsonl":
                scratch = (output / "jsonl" / "shards").resolve()
                if scratch.is_relative_to(output) and scratch.is_dir():
                    shutil.rmtree(scratch)
        report["outputs"] = compare_outputs(output / "jsonl", output / "parquet")
        counts = [m["result"]["rows_per_pass"] for m in report["measurements"]]
        if counts != [[manifest["rows"]] * 2] * 2:
            raise RuntimeError("Scoring skipped input rows; parity cannot establish complete coverage")
        for name, path in originals.items():
            if fingerprint(path) != signatures[name]:
                raise RuntimeError(f"Input changed during scoring: {name}")
        for name, path in inputs.items():
            if name != "activity" and fingerprint(path) != signatures[name]:
                raise RuntimeError(f"Frozen input changed during scoring: {name}")
        if _dataset_fingerprints(dataset) != parquet_signatures:
            raise RuntimeError("Parquet snapshot changed during scoring")
        if _code_identity() != report["code"]:
            raise RuntimeError("Scoring code changed during the experiment")
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        request, response = map(Path, sys.argv[2:4])
        value = score(json.loads(request.read_text(encoding="utf-8")))
        response.write_text(json.dumps(value), encoding="utf-8")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--rss-limit-mb", type=int, default=4096)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    report = run_shadow(**vars(args))
    log.info("shadow comparison %s", report["status"])


if __name__ == "__main__":
    main()
