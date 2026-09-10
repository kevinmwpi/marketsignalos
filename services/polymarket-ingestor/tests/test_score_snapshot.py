from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import score_snapshot as snapshot
from marketsignalos_polymarket.models import PolymarketWalletBet, PolymarketWalletEnrichment
from marketsignalos_polymarket.runner import _Stores


def _enrichment() -> PolymarketWalletEnrichment:
    return PolymarketWalletEnrichment(
        proxy_wallet="0x123", name="wallet", pseudonym="", resolved_trades=1,
        wins=1, losses=0, win_rate=1, skill_likelihood=0.5, stddevs_above_expected=0,
        edge_mean=0, edge_lower_bound=-1, effective_sample_size=1,
        resolved_volume_usdc=1, rank_score=0, total_volume_usdc=1, total_pnl_usdc=1,
        avg_position_size_usdc=1, trade_count=1, last_activity_at=1,
    )


def _bet() -> PolymarketWalletBet:
    return PolymarketWalletBet(
        proxy_wallet="0x123", condition_id="condition", outcome_index=0, outcome="Yes",
        event_slug="event", category="Politics", title="Question", status="open",
        entry_price=.5, cost_usdc=1, net_size=2, realized_pnl_usdc=0,
        total_pnl_usdc=0, clv=None, last_trade_ts=1,
    )


def _score(stores: _Stores) -> int:
    # Use the real store interfaces to exercise the redirected write paths.
    with stores.wallet_bets.rewrite() as sink:
        assert sink([_bet()]) == 1
    return stores.enrichment.write_enrichment([_enrichment()])


def test_real_scorer_publishes_through_pilot_without_touching_prior_outputs(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import lean_pilot, runner

    monkeypatch.setattr(snapshot, "run_enrichment", runner.run_enrichment)
    activity = {"proxy_wallet": "0x" + "1" * 40, "timestamp": 1704067200,
                "condition_id": "condition", "type": "TRADE", "side": "BUY",
                "size": 10, "usdc_size": 4, "outcome_index": 0,
                "event_slug": "event", "transaction_hash": "transaction",
                "fetched_at": "2024-01-02T00:00:00+00:00"}
    (data / snapshot.INPUTS[0]).write_text(json.dumps(activity) + "\n", encoding="utf-8")
    market = {"condition_id": "condition", "outcomes": ["Yes", "No"],
              "outcome_prices": [0.6, 0.4], "active": True, "closed": False,
              "fetched_at": "2024-01-02T00:00:00+00:00", "category": "Politics"}
    (data / "polymarket_markets.jsonl").write_text(json.dumps(market) + "\n", encoding="utf-8")

    def execute(stage: str, root: Path, config: lean_pilot.PilotConfig,
                run_id: str) -> dict[str, Any]:
        if stage == "collect":
            return {"status": "succeeded", "fixture": True}
        return lean_pilot._execute_stage(stage, root, config, run_id)

    result = lean_pilot.run_cycle(data, lean_pilot.PilotConfig(), "integration", execute=execute)
    assert result["status"] == "succeeded"
    manifest = snapshot.load_current(data / "score-snapshots")
    assert manifest["files"][snapshot.ENRICHMENT]["rows"] == 1
    assert manifest["files"][snapshot.BETS]["rows"] == 1
    assert (data / snapshot.ENRICHMENT).read_text() == "old score bytes"
    assert (data / snapshot.BETS).read_text() == "old bet bytes"
    assert (data / "polymarket_activity.jsonl.index.json").read_text() == "not JSON"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    for name in snapshot.INPUTS:
        (root / name).write_text("", encoding="utf-8")
    # Scoring must never parse the activity dedupe index or replace old outputs.
    (root / "polymarket_activity.jsonl.index.json").write_text("not JSON", encoding="utf-8")
    (root / snapshot.ENRICHMENT).write_text("old score bytes", encoding="utf-8")
    (root / snapshot.BETS).write_text("old bet bytes", encoding="utf-8")
    monkeypatch.setattr(snapshot, "run_enrichment", _score)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return root


def test_publish_validates_and_commits_pointer_last(data: Path, tmp_path: Path) -> None:
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    snapshots = tmp_path / "snapshots"
    result = snapshot.score_snapshot(data, snapshots, "run-1")
    assert result["snapshot_kind"] == "derived_scores"
    assert result["run_id"] == "run-1"
    assert result["files"][snapshot.ENRICHMENT]["rows"] == 1
    assert result["files"][snapshot.BETS]["rows"] == 1
    assert result["files"][snapshot.ENRICHMENT]["score_versions"] == ["forecast-v4"]
    assert len(result["code"]["package_source_sha256"]) == 64
    assert datetime.fromisoformat(result["completed_at"]).utcoffset() is not None
    assert snapshot.load_current(snapshots) == result
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    assert {path.name for path in (snapshots / "run-1").iterdir()} == {
        "manifest.json", snapshot.ENRICHMENT, snapshot.BETS,
    }
    with pytest.raises(FileExistsError):
        snapshot.score_snapshot(data, snapshots, "run-1")
    assert snapshot.load_current(snapshots) == result


def test_failure_before_publish_retains_previous_generation(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = tmp_path / "snapshots"
    first = snapshot.score_snapshot(data, snapshots, "first")
    pointer = (snapshots / "current.json").read_bytes()

    def fail(stores: _Stores) -> int:
        stores.wallet_bets.write_bets([_bet()])
        raise RuntimeError("scoring stopped")

    monkeypatch.setattr(snapshot, "run_enrichment", fail)
    with pytest.raises(RuntimeError, match="scoring stopped"):
        snapshot.score_snapshot(data, snapshots, "failed")
    assert (snapshots / "current.json").read_bytes() == pointer
    assert not (snapshots / "failed" / "manifest.json").exists()
    assert snapshot.load_current(snapshots) == first


@pytest.mark.parametrize("failure", ["count", "bad_json", "bet_count", "schema", "nan", "inf"])
def test_invalid_output_cannot_publish(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    snapshots = tmp_path / "snapshots"
    first = snapshot.score_snapshot(data, snapshots, "first")

    def invalid(stores: _Stores) -> int:
        count = _score(stores)
        if failure == "count":
            return count + 1
        if failure == "nan":
            return stores.enrichment.write_enrichment([replace(_enrichment(), edge_mean=float("nan"))])
        target = snapshots / failure / snapshot.ENRICHMENT
        if failure == "bad_json":
            target.write_text("{broken\n", encoding="utf-8")
        elif failure == "bet_count":
            (snapshots / failure / snapshot.BETS).write_bytes(b"")
        elif failure == "schema":
            target.write_text('{"proxy_wallet":"0x123"}\n', encoding="utf-8")
        elif failure == "inf":
            target.write_text(target.read_text(encoding="utf-8").replace(
                '"edge_mean":0', '"edge_mean":1e999'), encoding="utf-8")
        return count

    monkeypatch.setattr(snapshot, "run_enrichment", invalid)
    with pytest.raises(ValueError):
        snapshot.score_snapshot(data, snapshots, failure)
    assert not (snapshots / failure / "manifest.json").exists()
    assert snapshot.load_current(snapshots) == first


def test_source_change_refuses_publish(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = tmp_path / "snapshots"
    first = snapshot.score_snapshot(data, snapshots, "first")

    def changed(stores: _Stores) -> int:
        result = _score(stores)
        (data / snapshot.INPUTS[1]).write_text("a concurrent market observation", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot, "run_enrichment", changed)
    with pytest.raises(RuntimeError, match="inputs changed"):
        snapshot.score_snapshot(data, snapshots, "changed")
    assert not (snapshots / "changed" / "manifest.json").exists()
    assert snapshot.load_current(snapshots) == first


def test_pointer_failure_keeps_previous_and_leaves_completed_orphan(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = tmp_path / "snapshots"
    first = snapshot.score_snapshot(data, snapshots, "first")
    original = snapshot._write_json_atomic

    def fail_pointer(path: Path, payload: dict[str, Any], run_id: str) -> str:
        if path.name == "current.json":
            raise OSError("pointer unavailable")
        return original(path, payload, run_id)

    monkeypatch.setattr(snapshot, "_write_json_atomic", fail_pointer)
    with pytest.raises(OSError, match="pointer unavailable"):
        snapshot.score_snapshot(data, snapshots, "orphan")
    assert (snapshots / "orphan" / "manifest.json").exists()
    assert snapshot.load_current(snapshots) == first


def test_load_rejects_corrupted_snapshot(data: Path, tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    snapshot.score_snapshot(data, snapshots, "first")
    target = snapshots / "first" / snapshot.BETS
    row = json.loads(target.read_bytes())
    row["title"] = "changed"
    target.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum/count mismatch"):
        snapshot.load_current(snapshots)
    assert snapshot.load_current(snapshots, verify_files=False)["run_id"] == "first"


def test_missing_context_is_explicit_but_missing_activity_fails(data: Path, tmp_path: Path) -> None:
    (data / snapshot.INPUTS[1]).unlink()
    snapshots = tmp_path / "snapshots"
    result = snapshot.score_snapshot(data, snapshots, "context-missing")
    assert result["inputs"][snapshot.INPUTS[1]] == {"exists": False}
    (data / snapshot.INPUTS[0]).unlink()
    with pytest.raises(ValueError, match="without an activity"):
        snapshot.score_snapshot(data, snapshots, "activity-missing")
    assert snapshot.load_current(snapshots) == result


@pytest.mark.parametrize("run_id", ["../outside", "", ".hidden", "a/b", "a\\b"])
def test_run_id_cannot_escape_snapshot_directory(data: Path, tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        snapshot.score_snapshot(data, tmp_path / "snapshots", run_id)
