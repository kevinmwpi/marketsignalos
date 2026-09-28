from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("duckdb") is None:
    pytest.skip("Install benchmark extra for frozen metadata audit", allow_module_level=True)

from marketsignalos_polymarket import metadata_audit as audit
from marketsignalos_polymarket.activity_parquet import build_dataset, fingerprint
from marketsignalos_polymarket.metadata_coverage import MetadataCoverage
from marketsignalos_polymarket.models import PolymarketWalletHydration
from marketsignalos_polymarket.runner import parse_activity_row, parse_market_row
from marketsignalos_polymarket.skill_computation import compute_enrichment_outputs

WALLET = "0x" + "ab" * 20


def test_replay_changes_only_metadata_status_and_reasons() -> None:
    row: dict[str, Any] = {"data_quality_reasons": [audit.METADATA_REASON, "incomplete activity history"],
           "tailability_reasons": [audit.METADATA_REASON, "incomplete activity history", "some model reason"],
           "data_quality_status": "untrusted", "tailability_status": "blocked", "unchanged": 3}
    repaired = audit.replay_metadata_gate(row, MetadataCoverage(2, 2, 0, 0))
    assert repaired["data_quality_reasons"] == ["incomplete activity history"]
    assert repaired["tailability_reasons"] == ["incomplete activity history", "some model reason"]
    assert repaired["data_quality_status"] == "untrusted"
    assert repaired["tailability_status"] == "blocked"
    assert repaired["unchanged"] == 3
    assert row["data_quality_reasons"][0] == audit.METADATA_REASON


def fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    event = parse_activity_row({"proxyWallet": WALLET, "conditionId": "one", "type": "TRADE",
                                "timestamp": 1000, "side": "BUY", "size": 10,
                                "price": .4, "usdcSize": 4, "outcomeIndex": 0})
    market = parse_market_row({"conditionId": "one", "closed": True,
                              "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'})
    hydration = PolymarketWalletHydration(
        proxy_wallet=WALLET, activity_history_complete=True, positions_complete=True,
        closed_positions_complete=True, economic_all_time_complete=True, economic_month_complete=True,
        metadata_condition_count=1, metadata_covered_count=0, metadata_coverage=0,
    )
    for name, row in (("activity", event), ("markets", market), ("wallet_hydration", hydration)):
        (data / f"polymarket_{name}.jsonl").write_text(json.dumps(asdict(row)) + "\n", encoding="utf-8")
    dataset = tmp_path / "dataset"
    manifest = build_dataset(data / "polymarket_activity.jsonl", dataset)
    scores, _ = compute_enrichment_outputs(activity=[event], markets=[market], leaderboard=[],
                                           hydration_by_wallet={WALLET: hydration})
    stale = audit.replay_metadata_gate(asdict(scores[0]), MetadataCoverage(1, 0, 1, 0))
    snapshot = tmp_path / "scores.jsonl"
    snapshot.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    evidence: dict[str, Any] = {
        "status": "passed", "measurements": [
            {"operation": engine, "result": {"wallets": 1, "tailable_wallets": 0,
             "outputs": {"enrichments": fingerprint(snapshot)}}} for engine in ("jsonl", "parquet")],
        "inputs": {name: fingerprint(data / f"polymarket_{file}.jsonl")
                   for name, file in (("activity", "activity"), ("markets", "markets"),
                                      ("hydration", "wallet_hydration"))},
        "dataset": {"files": {name: fingerprint(dataset / name)
                    for name in ["manifest.json", *[f["path"] for f in manifest["files"]]]}},
    }
    benchmark = tmp_path / "shadow.json"
    benchmark.write_text(json.dumps(evidence), encoding="utf-8")
    return snapshot, benchmark, data, dataset


def test_full_audit_reconciles_frozen_inputs_and_rejects_changed_sources(tmp_path: Path) -> None:
    snapshot, benchmark, data, dataset = fixture(tmp_path)
    before = {p: p.read_bytes() for p in [snapshot, benchmark, *data.iterdir()]}
    report, replay = audit.build_audit(snapshot, benchmark, data, dataset)
    assert report["summary"]["metadata_failures_before"] == 1
    assert report["summary"]["metadata_failures_after"] == 0
    assert report["summary"]["trusted_after"] == 1
    assert report["summary"]["hydration_count_mismatches"] == 1
    assert audit.METADATA_REASON not in replay[0]["tailability_reasons"]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    market_path = data / "polymarket_markets.jsonl"
    market_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen source"):
        audit.build_audit(snapshot, benchmark, data, dataset)


def test_changed_parquet_cannot_be_used_as_source(tmp_path: Path) -> None:
    snapshot, benchmark, data, dataset = fixture(tmp_path)
    parquet = next(dataset.rglob("*.parquet"))
    raw = bytearray(parquet.read_bytes())
    raw[len(raw) // 2] ^= 1
    parquet.write_bytes(raw)
    with pytest.raises(ValueError, match="Frozen source"):
        audit.build_audit(snapshot, benchmark, data, dataset)


def test_cli_does_not_overwrite_existing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot, benchmark, data, dataset = fixture(tmp_path)
    out = tmp_path / "report"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("keep")
    monkeypatch.setattr("sys.argv", ["metadata_audit", "--snapshot", str(snapshot), "--benchmark",
                                   str(benchmark), "--data-dir", str(data), "--dataset", str(dataset),
                                   "--output", str(out)])
    with pytest.raises(SystemExit):
        audit.main()
    assert sentinel.read_text() == "keep"
