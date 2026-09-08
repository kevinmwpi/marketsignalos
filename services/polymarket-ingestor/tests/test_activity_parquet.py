from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("duckdb") is None or importlib.util.find_spec("psutil") is None:
    pytest.skip("Install the benchmark extra to run storage tests", allow_module_level=True)
# Only optional dependencies may cause a skip. A broken import inside our own
# modules must fail collection instead of making the benchmark checks vacuous.
parquet = importlib.import_module("marketsignalos_polymarket.activity_parquet")
benchmark = importlib.import_module("marketsignalos_polymarket.storage_benchmark")
WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


def _row(**kwargs: Any) -> dict[str, Any]:
    return {"proxy_wallet": WALLET, "timestamp": 1704067200, "type": "TRADE", "side": "BUY",
            "size": 4, "usdc_size": 1.25, "fetched_at": "2024-01-01T00:00:00Z",
            "transaction_hash": "same-transaction", "unknown_future_field": {"preserve": True},
            **kwargs}


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    source = tmp_path / "source.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return source


def test_round_trip_preserves_payloads_duplicates_and_partition_mapping(tmp_path: Path) -> None:
    rows = [_row(), _row(), _row(proxy_wallet=OTHER, side="SELL", timestamp=1704153600)]
    source = _write(tmp_path, rows)
    before = source.read_bytes()
    output = tmp_path / "dataset's name"
    manifest = parquet.build_dataset(source, output)
    assert manifest["rows"] == 3
    assert manifest["deduplicated"] is False
    assert source.read_bytes() == before
    with parquet.connect() as db:
        stored = db.execute("""
            SELECT raw_json, wallet, wallet_bucket, source_record FROM read_parquet(?)
            ORDER BY source_record
        """, [str(output / "activity" / "**" / "*.parquet")]).fetchall()
    assert [json.loads(row[0]) for row in stored] == rows
    assert [row[3] for row in stored] == [1, 2, 3]
    assert all(row[2] == parquet.wallet_bucket(row[1]) for row in stored)
    assert parquet.query_activity(output, WALLET.upper()) == [2, 2, 2.5, 1704067200, 1704067200]
    assert parquet.query_activity(output, "0x" + "00" * 20) == [0, 0, 0.0, None, None]


def test_as_of_excludes_late_backfill_future_event_and_unknown_observation(tmp_path: Path) -> None:
    source = _write(tmp_path, [
        _row(),
        _row(fetched_at="2024-02-01T00:00:00Z"),  # Old trade learned about later.
        _row(timestamp=1735689600),  # Future event with an invalidly early observation.
        _row(fetched_at=None),
        _row(fetched_at="2024-01-01T00:00:00"),  # Ambiguous timezone is not guessed.
        _row(fetched_at="invalidZ"),
        _row(fetched_at="2024-01-01T00:00:00+0000"),
    ])
    output = tmp_path / "dataset"
    manifest = parquet.build_dataset(source, output)
    assert manifest["unknown_observed_at_rows"] == 4
    query = {"wallet": WALLET, "since": 0, "as_of": "2024-01-02T00:00:00Z"}
    expected = [1, 1, 1.25, 1704067200, 1704067200]
    assert parquet.query_activity(output, **query) == expected
    assert benchmark.jsonl_queries(source, [query]) == [expected]
    with pytest.raises(ValueError, match="timezone"):
        parquet.query_activity(output, WALLET, as_of="2024-01-02")


@pytest.mark.parametrize("bad", [{"proxy_wallet": "bad"}, {"timestamp": None},
                                 {"timestamp": 1704067200.25},
                                 {"usdc_size": "Infinity"}])
def test_bad_required_data_cannot_publish_manifest(tmp_path: Path, bad: dict[str, Any]) -> None:
    source = _write(tmp_path, [_row(**bad)])
    output = tmp_path / "dataset"
    with pytest.raises(ValueError, match="Invalid required"):
        parquet.build_dataset(source, output)
    assert not (output / "manifest.json").exists()


def test_no_overwrite_or_partial_dataset_reads(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row()])
    output = tmp_path / "dataset"
    output.mkdir()
    with pytest.raises(FileExistsError):
        parquet.build_dataset(source, output)
    with pytest.raises(FileNotFoundError):
        parquet.query_activity(output, WALLET)


def test_changed_inventory_and_source_rejected(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row()])
    output = tmp_path / "dataset"
    manifest = parquet.build_dataset(source, output)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_row()) + "\n")
    with pytest.raises(RuntimeError, match="Source changed"):
        parquet.assert_source_unchanged(source, manifest["source_fingerprint"])
    file = next((output / "activity").rglob("*.parquet"))
    file.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="inventory"):
        parquet.load_manifest(output)


def test_extra_partition_file_is_rejected(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row()])
    output = tmp_path / "dataset"
    parquet.build_dataset(source, output)
    original = next((output / "activity").rglob("*.parquet"))
    (original.parent / "unexpected.parquet").write_bytes(original.read_bytes())
    with pytest.raises(ValueError, match="inventory"):
        parquet.query_activity(output, WALLET)


def test_partition_predicate_is_present_in_query_plan(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row(), _row(proxy_wallet=OTHER)])
    output = tmp_path / "dataset"
    parquet.build_dataset(source, output)
    sql, params = parquet.activity_query(output, WALLET)
    with parquet.connect() as db:
        plan = db.execute("EXPLAIN " + sql, params).fetchone()[1]
    assert "File Filters" in plan
    assert "Scanning Files: 1/2" in plan


def test_benchmark_runs_in_children_and_checks_correctness(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row(), _row(proxy_wallet=OTHER)])
    report = benchmark.run_benchmark(source, tmp_path / "benchmark", repetitions=1)
    assert report["status"] == "passed"
    assert len(report["measurements"]) == 2
    assert all(row["peak_rss_bytes"] > 0 for row in report["measurements"])
    assert (tmp_path / "benchmark" / "report.json").is_file()
    assert not benchmark.results_match([[1, 1, 2.0, 1, 1]], [[2, 1, 2.0, 1, 1]])
    reused = benchmark.run_benchmark(source, tmp_path / "rerun", repetitions=1,
                                     reuse_dataset=tmp_path / "benchmark" / "dataset")
    assert reused["status"] == "passed"
    assert "reused_dataset" in reused["conversion"]


def test_resource_guard_monitors_actual_interpreter_and_records_failure(tmp_path: Path) -> None:
    source = _write(tmp_path, [_row()])
    with pytest.raises(RuntimeError, match="RSS guard"):
        benchmark.run_benchmark(source, tmp_path / "bounded", repetitions=1, rss_limit_mb=16)
    report = json.loads((tmp_path / "bounded" / "report.json").read_text())
    assert report["status"] == "failed"
    assert "summary" not in report
