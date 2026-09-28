"""Reproducible offline JSONL/Parquet benchmark, with process resource guards."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from .activity_parquet import (
    activity_query,
    assert_source_unchanged,
    build_dataset,
    connect,
    fingerprint,
    load_manifest,
    parse_cutoff,
    parse_observation,
)

log = logging.getLogger("marketsignalos.storage_benchmark")


def jsonl_queries(source: Path, queries: list[dict[str, Any]]) -> list[list[Any]]:
    """One streaming json.loads pass evaluates the whole workload bundle.

    This matches the existing full-scan access pattern without constructing
    millions of Python model objects. It is not the latency of an API endpoint.
    """
    by_wallet: dict[str, list[int]] = {}
    cutoffs = [parse_cutoff(q["as_of"]) if q.get("as_of") else None for q in queries]
    result: list[list[Any]] = [[0, 0, 0.0, None, None] for _ in queries]
    compensation = [0.0] * len(queries)
    for i, query in enumerate(queries):
        by_wallet.setdefault(query["wallet"].lower(), []).append(i)
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            indices = by_wallet.get(str(row.get("proxy_wallet", "")).lower())
            if not indices:
                continue
            ts = int(row["timestamp"])
            observed = parse_observation(row.get("fetched_at", ""))
            for i in indices:
                if ts < queries[i]["since"]:
                    continue
                cutoff = cutoffs[i]
                if cutoff is not None and (
                    ts > cutoff.timestamp() or observed is None or observed > cutoff
                ):
                    continue
                values = result[i]
                values[0] += 1
                values[1] += int(str(row.get("type", "")).upper() == "TRADE"
                                 and str(row.get("side", "")).upper() == "BUY")
                adjusted = float(row.get("usdc_size") or 0) - compensation[i]
                total = values[2] + adjusted
                compensation[i] = (total - values[2]) - adjusted
                values[2] = total
                values[3] = ts if values[3] is None else min(values[3], ts)
                values[4] = ts if values[4] is None else max(values[4], ts)
    return result


def parquet_queries(dataset: Path, queries: list[dict[str, Any]], *,
                    memory_mb: int, threads: int) -> list[list[Any]]:
    load_manifest(dataset)
    results = []
    with connect(memory_mb=memory_mb, threads=threads) as db:
        for query in queries:
            sql, params = activity_query(dataset, **query)
            row = db.execute(sql, params).fetchone()
            assert row is not None
            results.append(list(row))
    return results


def results_match(left: list[list[Any]], right: list[list[Any]]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        a[:2] == b[:2] and a[3:] == b[3:]
        and math.isclose(a[2], b[2], rel_tol=1e-10, abs_tol=1e-7)
        for a, b in zip(left, right, strict=True)
    )


def _worker(job: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    value: Any
    if job["operation"] == "build":
        value = build_dataset(Path(job["source"]), Path(job["dataset"]),
                              memory_mb=job["memory_mb"], threads=job["threads"])
    elif job["operation"] == "jsonl":
        value = jsonl_queries(Path(job["source"]), job["queries"])
    elif job["operation"] == "parquet":
        value = parquet_queries(Path(job["dataset"]), job["queries"],
                                memory_mb=job["memory_mb"], threads=job["threads"])
    else:
        raise ValueError("Unknown benchmark operation")
    info = psutil.Process().memory_info()
    return {"elapsed_seconds": time.perf_counter() - start, "result": value,
            "os_peak_rss_bytes": getattr(info, "peak_wset", None)}


def measured_job(job: dict[str, Any], directory: Path, name: str, *,
                 rss_limit_mb: int, timeout_seconds: int,
                 worker_module: str = "marketsignalos_polymarket.storage_benchmark") -> dict[str, Any]:
    request = directory / f"{name}.job.json"
    response = directory / f"{name}.result.json"
    request.write_text(json.dumps(job), encoding="utf-8")
    peak = 0
    started = time.perf_counter()
    with (directory / f"{name}.log").open("w", encoding="utf-8") as errors, subprocess.Popen(
        [sys.executable, "-m", worker_module,
         "_worker", str(request), str(response)], stdout=errors, stderr=errors,
    ) as process:
        monitor = psutil.Process(process.pid)
        failure = None
        try:
            while process.poll() is None:
                total_rss = 0
                try:
                    # Windows venv python.exe can launch the actual interpreter
                    # as a child. Monitoring only the launcher misses its memory.
                    members = [monitor, *monitor.children(recursive=True)]
                except psutil.NoSuchProcess:
                    break
                for member in members:
                    try:
                        total_rss += member.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, total_rss)
                if peak > rss_limit_mb * 1024**2:
                    failure = f"Worker exceeded {rss_limit_mb} MiB sampled RSS guard"
                if time.perf_counter() - started > timeout_seconds:
                    failure = f"Worker exceeded {timeout_seconds}s timeout"
                if failure:
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                try:
                    children = monitor.children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                if process.poll() is None:
                    process.kill()
                psutil.wait_procs(children, timeout=3)
            code = process.wait()
    if code != 0 or failure:
        raise RuntimeError(failure or f"Worker {name} failed; see {name}.log")
    value: dict[str, Any] = json.loads(response.read_text(encoding="utf-8"))
    value["sampled_peak_rss_bytes"] = peak
    value["peak_rss_bytes"] = max(peak, value.get("os_peak_rss_bytes") or 0)
    value["process_wall_seconds"] = time.perf_counter() - started
    value["operation"] = job["operation"]
    response.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    log.info("%s finished seconds=%.3f peak_rss_mib=%.1f", name,
             value["elapsed_seconds"], value["peak_rss_bytes"] / 1024**2)
    return value


def make_queries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    selected = [row["wallet"] for row in manifest["selected_wallets"]]
    queries = []
    as_of = manifest["max_observed_at"]
    if as_of:
        as_of = (parse_cutoff(as_of) - timedelta(days=7)).isoformat()
    for wallet in selected:
        queries.extend([
            {"wallet": wallet, "since": 0},
            {"wallet": wallet, "since": max(0, manifest["max_event_ts"] - 30 * 86400)},
        ])
        if as_of:
            queries.append({"wallet": wallet, "since": 0, "as_of": as_of})
    return queries


def run_benchmark(source: Path, output: Path, *, repetitions: int = 3,
                  memory_mb: int = 512, threads: int = 2, rss_limit_mb: int = 2048,
                  timeout_seconds: int = 1800,
                  reuse_dataset: Path | None = None) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("At least one repetition is required")
    source = source.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    dataset = reuse_dataset.resolve(strict=True) if reuse_dataset else output / "dataset"
    job: dict[str, Any] = {"source": str(source), "dataset": str(dataset),
                           "memory_mb": memory_mb, "threads": threads}
    report: dict[str, Any] = {
        "status": "running", "started_at": datetime.now(UTC).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "logical_cpus": psutil.cpu_count(),
                        "total_ram_bytes": psutil.virtual_memory().total},
        "settings": {**job, "rss_limit_mb": rss_limit_mb,
                     "timeout_seconds": timeout_seconds, "repetitions": repetitions},
        "cache_policy": "fresh child processes; OS page cache not flushed; alternating engine order",
        "scope": "local activity projection and aggregate workload; no API or full enrichment timing",
        "code_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in ("activity_parquet.py", "storage_benchmark.py")},
    }
    try:
        if reuse_dataset:
            manifest = load_manifest(dataset)
            if fingerprint(source) != manifest["source_fingerprint"]:
                raise RuntimeError("Source fingerprint does not match reused dataset")
            build: dict[str, Any] = {"reused_dataset": str(dataset)}
        else:
            build = measured_job({**job, "operation": "build"}, output, "build", rss_limit_mb=rss_limit_mb, timeout_seconds=timeout_seconds)
            manifest = build.pop("result")
        report["dataset"] = manifest
        report["conversion"] = build
        queries = make_queries(manifest)
        report["queries"] = queries
        measurements = []
        reference = None
        for repetition in range(repetitions):
            engines = ["jsonl", "parquet"] if repetition % 2 == 0 else ["parquet", "jsonl"]
            for engine in engines:
                assert_source_unchanged(source, manifest["source_fingerprint"])
                run = measured_job({**job, "operation": engine, "queries": queries},
                                   output, f"{engine}-{repetition}", rss_limit_mb=rss_limit_mb, timeout_seconds=timeout_seconds)
                result = run.pop("result")
                if reference is None:
                    reference = result
                elif not results_match(reference, result):
                    raise RuntimeError("Query results differ; performance comparison rejected")
                run["repetition"] = repetition
                measurements.append(run)
        if fingerprint(source) != manifest["source_fingerprint"]:
            raise RuntimeError("Source fingerprint changed during benchmark")
        report["measurements"] = measurements
        report["results"] = reference
        medians = {engine: statistics.median(r["elapsed_seconds"] for r in measurements
                                           if r["operation"] == engine)
                   for engine in ("jsonl", "parquet")}
        report["summary"] = {
            "median_seconds": medians,
            "query_bundle_speedup": medians["jsonl"] / medians["parquet"],
            "jsonl_to_parquet_bytes_ratio": manifest["source_fingerprint"]["bytes"]
                                           / manifest["parquet_bytes"],
            "correctness": "counts and timestamps exact; sums within rel=1e-10, abs=1e-7",
        }
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
        result = _worker(json.loads(request.read_text(encoding="utf-8")))
        response.write_text(json.dumps(result), encoding="utf-8")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--rss-limit-mb", type=int, default=2048)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--reuse-dataset", type=Path)
    args = parser.parse_args()
    report = run_benchmark(**vars(args))
    log.info("benchmark passed summary=%s", report["summary"])


if __name__ == "__main__":
    main()
