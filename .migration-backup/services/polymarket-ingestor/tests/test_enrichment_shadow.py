from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("duckdb") is None or importlib.util.find_spec("psutil") is None:
    pytest.skip("Install the benchmark extra to run shadow tests", allow_module_level=True)
parquet = importlib.import_module("marketsignalos_polymarket.activity_parquet")
reader_module = importlib.import_module("marketsignalos_polymarket.activity_reader")
shadow = importlib.import_module("marketsignalos_polymarket.enrichment_shadow")
runner = importlib.import_module("marketsignalos_polymarket.runner")


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def snapshot(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    wallets = [f"0x{i:040x}" for i in range(80)]  # Collisions and many partitions.
    rows = []
    for i, wallet in enumerate(wallets):
        for j, (ts, side, size, price) in enumerate([
            (1704067300, "BUY", 10, .4), (1704067200, "BUY", 5, .2),
            (1704067300, "SELL", 2, .6),
        ]):
            rows.append({"proxy_wallet": wallet.upper() if j == 1 else wallet,
                         "timestamp": ts, "type": "TRADE", "side": side,
                         "size": size, "usdc_size": size * price, "price": price,
                         "condition_id": f"c{i % 3}", "outcome_index": i % 2,
                         "outcome": "Yes" if i % 2 == 0 else "No", "title": f"title {j}",
                         "event_slug": f"event{i % 3}", "transaction_hash": f"tx{i}-{j}",
                         "fetched_at": "2024-01-02T00:00:00Z"})
    # Duplicate and out-of-order events must survive unchanged.
    rows.insert(50, dict(rows[0]))
    write_rows(data / shadow.INPUTS["activity"], rows)
    write_rows(data / shadow.INPUTS["markets"], [
        {"condition_id": f"c{i}", "fetched_at": stamp, "category": "Politics",
         "outcomes": ["Yes", "No"], "outcome_prices": prices,
         "active": True, "closed": closed, "end_date": "2024-01-10T00:00:00Z"}
        for i in range(3)
        for stamp, prices, closed in [
            ("2024-01-01T12:00:00Z", [.6, .4], False),
            ("2024-01-11T00:00:00Z", [1., 0.] if i < 2 else [.65, .35], i < 2),
        ]
    ])
    write_rows(data / shadow.INPUTS["prices"], [
        {"condition_id": f"c{i}", "fetched_at": "2024-01-02T00:00:00Z",
         "yes_price": .7, "active": True, "closed": False} for i in range(3)
    ])
    write_rows(data / shadow.INPUTS["leaderboard"], [
        {"proxy_wallet": w, "name": f"wallet{i}", "pseudonym": "p"}
        for i, w in enumerate(wallets)
    ])
    write_rows(data / shadow.INPUTS["hydration"], [
        {"proxy_wallet": w, "activity_history_complete": True,
         "positions_complete": True, "closed_positions_complete": True,
         "economic_all_time_complete": True, "economic_month_complete": True,
         "all_time_pnl_usdc": 200, "all_time_volume_usdc": 1000,
         "metadata_coverage": 1} for w in wallets
    ])
    dataset = tmp_path / "dataset"
    parquet.build_dataset(data / shadow.INPUTS["activity"], dataset)
    return data, dataset


def test_reader_preserves_every_field_order_and_replays(snapshot: tuple[Path, Path],
                                                       tmp_path: Path) -> None:
    data, dataset = snapshot
    paths = runner._shard_activity_by_wallet(data / shadow.INPUTS["activity"], shard_count=64,
                                             tmp_dir=tmp_path / "shards")

    def normalize(shards: Any) -> list[list[dict[str, Any]]]:
        result = []
        for shard in shards:
            rows = []
            for record in shard:
                row = asdict(record)
                # The legacy row parser creates a new wall-clock fetched_at;
                # it is not used by scoring. Original raw_json remains intact.
                row.pop("fetched_at")
                rows.append(row)
            result.append(rows)
        return result

    expected = normalize(runner._iter_activity_shards(paths))
    reader = reader_module.ParquetActivityReader(dataset)
    assert normalize(reader()) == expected
    assert normalize(reader()) == expected
    assert sum(map(len, expected)) == 241


def test_full_scoring_exact_parity_in_guarded_workers(snapshot: tuple[Path, Path],
                                                     tmp_path: Path) -> None:
    data, dataset = snapshot
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    output = tmp_path / "shadow"
    report = shadow.run_shadow(data, dataset, output)
    assert report["status"] == "passed"
    left, right = [m["result"] for m in report["measurements"]]
    assert left["rows_per_pass"] == right["rows_per_pass"] == [241, 241]
    assert left["wallets"] == right["wallets"] == 80
    assert left["bets"] == right["bets"] == 80
    assert left["resolved_bets"] > 0
    assert all(m["peak_rss_bytes"] > 0 for m in report["measurements"])
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    assert not (output / "jsonl" / "shards").exists()
    # A meaningful field change must fail, even if every row count agrees.
    target = output / "parquet" / "enrichments.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["rank_score"] += .00000001
    lines[0] = json.dumps(row)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="enrichments differ at canonical output line 1"):
        shadow.compare_outputs(output / "jsonl", output / "parquet")


def test_reader_rejects_mutation_between_passes(snapshot: tuple[Path, Path]) -> None:
    _, dataset = snapshot
    reader = reader_module.ParquetActivityReader(dataset)
    list(reader())
    manifest = dataset / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        list(reader())


def test_wrong_source_fails_before_scoring(snapshot: tuple[Path, Path], tmp_path: Path) -> None:
    data, dataset = snapshot
    source = data / shadow.INPUTS["activity"]
    with source.open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="fingerprint"):
        shadow.run_shadow(data, dataset, output)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["measurements"] == []
    assert "outputs" not in report


def test_missing_context_and_output_overlap_fail_closed(snapshot: tuple[Path, Path],
                                                       tmp_path: Path) -> None:
    data, dataset = snapshot
    with pytest.raises(ValueError, match="outside"):
        shadow.run_shadow(data, dataset, data / "output")
    (data / shadow.INPUTS["markets"]).unlink()
    with pytest.raises(FileNotFoundError):
        shadow.run_shadow(data, dataset, tmp_path / "missing")
    with pytest.raises(FileExistsError):
        shadow.run_shadow(data, dataset, tmp_path / "missing")


def test_guard_failure_is_not_parity(snapshot: tuple[Path, Path], tmp_path: Path) -> None:
    data, dataset = snapshot
    output = tmp_path / "limited"
    with pytest.raises(RuntimeError, match="RSS guard"):
        shadow.run_shadow(data, dataset, output, rss_limit_mb=16)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "outputs" not in report


def test_large_payload_replay_with_small_duckdb_budget(tmp_path: Path) -> None:
    # Raw payload sorting previously exhausted DuckDB's buffer limit. Keep
    # payload size large but the resulting model slice small in this regression.
    source = tmp_path / "large.jsonl"
    count = 18000
    with source.open("w", encoding="utf-8") as handle:
        for ts in range(count, 0, -1):
            row = {"proxy_wallet": "0x" + "ab" * 20, "timestamp": ts,
                   "type": "TRADE", "side": "BUY", "size": 1, "usdc_size": .4,
                   "price": .4, "future_payload": "x" * 4096}
            handle.write(json.dumps(row) + "\n")
    dataset = tmp_path / "large-dataset"
    parquet.build_dataset(source, dataset)
    reader = reader_module.ParquetActivityReader(dataset, memory_mb=192, threads=1)
    times = [record.timestamp for shard in reader() for record in shard]
    assert times == list(range(count, 0, -1))
