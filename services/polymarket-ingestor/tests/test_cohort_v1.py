from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import cohort_v1
from marketsignalos_polymarket import score_snapshot as snapshot
from marketsignalos_polymarket.cohort_v1 import (
    build,
    classify,
    config_hash,
    match_comparison,
    members,
)
from marketsignalos_polymarket.runner import parse_activity_row, parse_market_row
from marketsignalos_polymarket.storage import JsonlActivityStore, JsonlMarketStore

CUTOFF = datetime(2026, 10, 27, tzinfo=UTC)
DAY = 86400
WALLET = "0x" + "1" * 40
CLV = "fewer than 10 1 h post-entry CLV observations"


# ── Tiers ────────────────────────────────────────────────────────────────────

def _row(status: str, quality: str, reasons: list[str]) -> dict[str, Any]:
    return {"tailability_status": status, "data_quality_status": quality,
            "tailability_reasons": reasons}


def test_tiers_follow_the_gates_and_nest() -> None:
    assert classify(_row("tailable", "trusted", [])) == ("T1", [])
    assert classify(_row("blocked", "trusted", [CLV]))[0] == "T2"
    assert classify(_row("blocked", "trusted",
                         ["1 h post-entry CLV value not confidently positive"]))[0] == "T2"
    # A model gate fails as well as gate 13: data trusted, so T3 only.
    assert classify(_row("blocked", "trusted",
                         ["forecast confidence below 80%", CLV]))[0] == "T3"
    # Any data gate failing leaves the wallet out of every tier.
    assert classify(_row("blocked", "untrusted",
                         ["incomplete market metadata", CLV]))[0] == "none"


# ── The comparison set ───────────────────────────────────────────────────────

def test_matching_prefers_category_and_tercile_then_tercile_then_nearest() -> None:
    buys = {"t2a": 10, "t2b": 50, "t2c": 90,
            "c1": 11, "c2": 49, "c3": 52, "c4": 200}
    category = {"t2a": "sports", "t2b": "politics", "t2c": "crypto",
                "c1": "sports", "c2": "sports", "c3": "politics", "c4": "crypto"}
    pool = ["t2a", "t2b", "t2c", "c1", "c2", "c3", "c4"]
    pairs, quality = match_comparison(["t2a", "t2b", "t2c"], pool, category=category,
                                      buys=buys)
    assert pairs == {"t2a": "c1", "t2b": "c3", "t2c": "c4"}
    assert quality["category_and_tercile"] == 3
    # With the category's only candidate taken, the same tercile wins, nearest count.
    pairs, quality = match_comparison(["t2a", "t2x"], ["t2a", "t2x", "c1", "c2"],
                                      category={"t2a": "sports", "t2x": "sports",
                                                "c1": "sports", "c2": "politics"},
                                      buys={"t2a": 10, "t2x": 12, "c1": 11, "c2": 13})
    assert pairs["t2a"] == "c1" and pairs["t2x"] == "c2"
    assert quality["unmatched"] == 0
    # More members than candidates: the remainder is reported, never doubled up.
    pairs, quality = match_comparison(["a", "b"], ["a", "b", "c"], category={},
                                      buys={})
    assert len(set(pairs.values())) == len(pairs) == 1 and quality["unmatched"] == 1


# ── The frozen configuration, end to end ─────────────────────────────────────

def _buy(cid: str, ts: int) -> dict[str, Any]:
    return {"proxyWallet": WALLET, "timestamp": ts, "conditionId": cid, "type": "TRADE",
            "side": "BUY", "size": 20, "usdcSize": 8, "price": 0.4, "outcomeIndex": 0,
            "eventSlug": f"event-{cid}", "transactionHash": f"0xt{cid}{ts}"}


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One wallet scored under forecast-v5, one excluded wallet, and one leaderboard
    wallet never hydrated."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = tmp_path / "data"
    root.mkdir()
    end = int(CUTOFF.timestamp()) + 30 * DAY
    JsonlMarketStore(root / "polymarket_markets.jsonl").write_markets([parse_market_row(
        {"id": "0xa", "conditionId": "0xa", "closed": False, "outcomes": '["Yes", "No"]',
         "outcomePrices": '["0.5", "0.5"]',
         "endDate": datetime.fromtimestamp(end, UTC).isoformat()})])
    activity = JsonlActivityStore(root / "polymarket_activity.jsonl")
    cutoff = int(CUTOFF.timestamp())
    activity.write_activity([parse_activity_row(_buy("0xa", ts)) for ts in (
        cutoff - 40 * DAY, cutoff - 3 * DAY, cutoff - DAY, cutoff + DAY)])
    activity.flush()
    (root / "excluded_wallets.txt").write_text(
        "0x" + "e" * 40 + "\t2026-10-05T00:00:00+00:00\tsystematic\n", encoding="utf-8")
    (root / "polymarket_leaderboard.jsonl").write_text(
        json.dumps({"proxy_wallet": "0x" + "f" * 40}) + "\n"
        + json.dumps({"proxy_wallet": WALLET}) + "\n", encoding="utf-8")
    snapshot.score_snapshot(root, root / "score-snapshots", "gen1",
                            score_version="forecast-v5")
    return root


def test_the_frozen_config_lists_every_screened_wallet_and_pins_its_inputs(
    data: Path,
) -> None:
    config = build(data, cutoff=CUTOFF, discovery={"leaderboard_metric": "profit"})
    screened = config["screened"]
    assert screened[WALLET]["tier"] in {"T1", "T2", "T3", "none"}
    assert screened[WALLET]["buys_30d"] == 2  # the 40-day-old and post-cutoff buys excluded
    assert screened["0x" + "e" * 40]["tier"] == "excluded"
    assert screened["0x" + "f" * 40]["tier"] == "unscreened"
    assert config["score_generation"]["run_id"] == "gen1"
    assert config["score_generation"]["code"]["package_source_sha256"]
    assert config["primary_tier"] == "T2" and config["window"]["evaluation_date"] is None
    # The hash covers every other key, and rebuilding from the same inputs repeats it.
    assert config["config_hash"] == config_hash(config)
    assert build(data, cutoff=CUTOFF, discovery={"leaderboard_metric": "profit"}
                 )["config_hash"] == config["config_hash"]
    changed = dict(config, primary_tier="T3")
    assert config_hash(changed) != config["config_hash"]
    roster = members(config)
    assert roster["config_hash"] == config["config_hash"]
    assert roster["wallets"] == sorted(set(roster["t2"]) | set(roster["comparison"]))


def test_a_generation_that_is_not_forecast_v5_is_refused(data: Path) -> None:
    snapshot.score_snapshot(data, data / "score-snapshots", "gen2",
                            score_version="forecast-v4")
    with pytest.raises(ValueError, match="forecast-v5"):
        build(data, cutoff=CUTOFF, discovery={})


def test_the_cli_writes_the_config_and_the_member_list(data: Path, tmp_path: Path) -> None:
    pilot = tmp_path / "pilot.json"
    pilot.write_text(json.dumps({"leaderboard_metric": "profit",
                                 "leaderboard_window": "month", "leaderboard_limit": 100,
                                 "max_watchlist_wallets": 64}))
    out, roster = tmp_path / "frozen-config.json", tmp_path / "members.json"
    assert cohort_v1.main(["--data-dir", str(data), "--pilot-config", str(pilot),
                           "--cutoff", CUTOFF.isoformat(), "--out", str(out),
                           "--members-out", str(roster)]) == 0
    config = json.loads(out.read_text())
    assert config["discovery"]["leaderboard_window"] == "month"
    assert json.loads(roster.read_text())["config_hash"] == config["config_hash"]
    with pytest.raises(SystemExit):
        cohort_v1.main(["--data-dir", str(data), "--pilot-config", str(pilot),
                        "--cutoff", "2026-10-27T00:00:00", "--out", str(out)])
