from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import cohort
from marketsignalos_polymarket.models import PolymarketWalletHydration
from marketsignalos_polymarket.runner import parse_activity_row
from marketsignalos_polymarket.score_snapshot import ENRICHMENT
from marketsignalos_polymarket.storage import (
    JsonlActivityStore,
    JsonlWalletHydrationStore,
    JsonWalletCheckpointStore,
)

KEEP, DROP = "0xkeep", "0xdrop"
NOW = datetime(2026, 10, 3, 8, 15, tzinfo=UTC)


def _trade(wallet: str, tx: str) -> dict[str, Any]:
    return {"proxyWallet": wallet, "timestamp": 1_790_000_000, "conditionId": "0xm",
            "type": "TRADE", "side": "BUY", "size": 10.0, "usdcSize": 4.0, "price": 0.4,
            "outcomeIndex": 0, "transactionHash": tx}


def _jsonl_wallets(path: Path) -> list[str]:
    wallets = []
    for line in path.read_text().splitlines():
        try:
            wallets.append(json.loads(line)["proxy_wallet"])
        except json.JSONDecodeError:
            continue  # the fixture's torn line
    return wallets


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Two wallets' data in every per-wallet store, written by the real stores."""
    activity = JsonlActivityStore(tmp_path / cohort.ACTIVITY_FILE)
    activity.write_activity([parse_activity_row(_trade(wallet, f"0xt{wallet}{i}"))
                             for wallet in (KEEP, DROP) for i in range(3)])
    activity.flush()
    with (tmp_path / cohort.ACTIVITY_FILE).open("a", encoding="utf-8") as out:
        out.write("{torn line\n")
    JsonlWalletHydrationStore(tmp_path / "polymarket_wallet_hydration.jsonl").upsert_hydration(
        [PolymarketWalletHydration(proxy_wallet=wallet) for wallet in (KEEP, DROP)])
    checkpoints = JsonWalletCheckpointStore(tmp_path / "polymarket_wallet_checkpoints.json")
    for wallet in (KEEP, DROP):
        checkpoints.set_last_timestamp(wallet, 1_790_000_000)
    (tmp_path / "polymarket_wallet_economics_cache.json").write_text(json.dumps(
        {KEEP: {"ALL": {"pnl_usdc": 1.0}}, DROP: {"ALL": {"pnl_usdc": 2.0}}}))
    for name in ("polymarket_positions.jsonl", "polymarket_wallet_values.jsonl"):
        (tmp_path / name).write_text("".join(
            json.dumps({"proxy_wallet": wallet, "n": i}) + "\n"
            for wallet in (KEEP, DROP.upper()) for i in range(2)))
    (tmp_path / cohort.WATCHLIST_FILE).write_text(f"# watchlist\n{KEEP}\n{DROP}\n")
    return tmp_path


def test_only_systematic_wallets_are_excluded() -> None:
    assert cohort.wallets_to_exclude([
        {"proxy_wallet": "0xA", "style_archetype": "systematic"},
        {"proxy_wallet": "0xb", "style_archetype": "mixed"},
        {"proxy_wallet": "0xc", "style_archetype": "discretionary"},
        {"proxy_wallet": "0xd", "style_archetype": "unclassified"},
    ]) == {"0xa": "systematic"}


def test_exclusions_only_grow(tmp_path: Path) -> None:
    assert cohort.record_exclusions(tmp_path, {DROP: "systematic"}, now=NOW) == 1
    assert cohort.record_exclusions(tmp_path, {DROP: "systematic", "0xb": "systematic"},
                                    now=NOW) == 1
    assert cohort.excluded_wallets(tmp_path) == {DROP, "0xb"}
    assert (tmp_path / cohort.EXCLUDED_FILE).read_text().splitlines()[0] == (
        f"{DROP}\t{NOW.isoformat()}\tsystematic")


def test_purging_removes_the_wallet_everywhere_and_keeps_dedupe_exact(data_dir: Path) -> None:
    removed = cohort.apply_exclusions(data_dir, frozenset({DROP}))

    assert removed == {
        cohort.WATCHLIST_FILE: 1, cohort.ACTIVITY_FILE: 3,
        "polymarket_positions.jsonl": 2, "polymarket_wallet_hydration.jsonl": 1,
        "polymarket_wallet_values.jsonl": 2, "polymarket_wallet_checkpoints.json": 1,
        "polymarket_wallet_economics_cache.json": 1,
    }
    assert (data_dir / cohort.WATCHLIST_FILE).read_text() == f"# watchlist\n{KEEP}\n"
    for name in (cohort.ACTIVITY_FILE, "polymarket_positions.jsonl",
                 "polymarket_wallet_hydration.jsonl", "polymarket_wallet_values.jsonl"):
        assert set(_jsonl_wallets(data_dir / name)) == {KEEP}, name
    assert "{torn line" in (data_dir / cohort.ACTIVITY_FILE).read_text()  # left as it was
    for name in cohort.WALLET_JSON_OBJECTS:
        assert set(json.loads((data_dir / name).read_text())) == {KEEP}, name

    # The rebuilt index still stops duplicates of kept rows, and only of those.
    store = JsonlActivityStore(data_dir / cohort.ACTIVITY_FILE)
    assert store.write_activity([parse_activity_row(_trade(KEEP, f"0xt{KEEP}0"))]) == 0
    assert store.write_activity([parse_activity_row(_trade(DROP, f"0xt{DROP}0"))]) == 1

    assert cohort.apply_exclusions(data_dir, frozenset({"0xnobody"})) == {}


def test_the_stage_excludes_what_scoring_labelled_and_finishes_interrupted_purges(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = data_dir / "score-snapshots" / "r1"
    generation.mkdir(parents=True)
    (data_dir / "score-snapshots" / "current.json").write_text("{}")
    (generation / ENRICHMENT).write_text("".join(json.dumps(row) + "\n" for row in (
        {"proxy_wallet": KEEP, "style_archetype": "discretionary"},
        {"proxy_wallet": DROP, "style_archetype": "systematic"},
    )))
    monkeypatch.setattr(cohort, "load_current", lambda snapshots: {"run_id": "r1"})

    result = cohort.run(data_dir, now=NOW)
    assert (result["wallets_classified"], result["excluded_new"], result["excluded_total"]) == (
        2, 1, 1)
    assert result["rows_removed"][cohort.ACTIVITY_FILE] == 3

    # A wallet excluded earlier whose purge never ran is purged on the next run.
    (data_dir / cohort.WATCHLIST_FILE).write_text(f"{KEEP}\n{DROP}\n")
    again = cohort.run(data_dir, now=NOW)
    assert again["excluded_new"] == 0 and again["rows_removed"] == {cohort.WATCHLIST_FILE: 1}


def test_a_score_generation_is_unprocessed_until_a_run_completes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = data_dir / "score-snapshots"
    assert not cohort.has_unprocessed_score(data_dir)  # nothing published yet
    (snapshots / "r1").mkdir(parents=True)
    (snapshots / "r1" / ENRICHMENT).write_text("")
    (snapshots / "current.json").write_text(json.dumps({"run_id": "r1"}))
    assert cohort.has_unprocessed_score(data_dir)

    def failing_purge(data_dir: Path, excluded: frozenset[str]) -> dict[str, int]:
        raise OSError("volume full")

    monkeypatch.setattr(cohort, "load_current", lambda snapshots: {"run_id": "r1"})
    with monkeypatch.context() as patch:
        patch.setattr(cohort, "apply_exclusions", failing_purge)
        with pytest.raises(OSError):
            cohort.run(data_dir, now=NOW)
    assert cohort.has_unprocessed_score(data_dir)  # an interrupted run counts for nothing
    cohort.run(data_dir, now=NOW)
    assert not cohort.has_unprocessed_score(data_dir)
    (snapshots / "current.json").write_text(json.dumps({"run_id": "r2"}))
    assert cohort.has_unprocessed_score(data_dir)
    (snapshots / "current.json").write_text("{torn")  # planning never raises
    assert not cohort.has_unprocessed_score(data_dir)


def test_the_stage_without_scores_only_applies_existing_exclusions(data_dir: Path) -> None:
    result = cohort.run(data_dir, now=NOW)
    assert result == {"status": "succeeded", "wallets_classified": 0, "excluded_new": 0,
                      "excluded_total": 0, "rows_removed": {}}


def test_frozen_cohort_members_are_never_excluded(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stage 3 (plan S2): membership is fixed at the cutoff for the whole window.
    generation = data_dir / "score-snapshots" / "r1"
    generation.mkdir(parents=True)
    (data_dir / "score-snapshots" / "current.json").write_text("{}")
    (generation / ENRICHMENT).write_text(json.dumps(
        {"proxy_wallet": DROP, "style_archetype": "systematic"}) + "\n")
    monkeypatch.setattr(cohort, "load_current", lambda snapshots: {"run_id": "r1"})

    result = cohort.run(data_dir, now=NOW, protected=frozenset({DROP}))
    assert result["excluded_new"] == 0 and result["rows_removed"] == {}
    assert DROP in _jsonl_wallets(data_dir / cohort.ACTIVITY_FILE)
