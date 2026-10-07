from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
    passes_clv_only,
)
from marketsignalos_polymarket.runner import parse_activity_row, parse_market_row
from marketsignalos_polymarket.storage import JsonlActivityStore, JsonlMarketStore

# Relative to now: the fixture's score generation is computed at test time, and a
# frozen config refuses a generation that started after its cutoff.
CUTOFF = datetime.now(UTC).replace(microsecond=0) + timedelta(days=20)
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


def test_the_clv_only_ablation_ignores_gates_7_to_12() -> None:
    model = "forecast confidence below 80%"
    assert passes_clv_only(_row("blocked", "trusted", [model]))  # T3, but CLV passes
    assert passes_clv_only(_row("tailable", "trusted", []))  # T1 passes everything
    assert not passes_clv_only(_row("blocked", "trusted", [CLV]))  # T2: CLV fails
    assert not passes_clv_only(_row("blocked", "untrusted", ["incomplete market metadata"]))


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
    assert roster["wallets"] == sorted(set(roster["t2"]) | set(roster["comparison"])
                                       | set(roster["clv_only"]))
    assert set(config["ablations"]["rules"]) == {"price_only", "clv_only",
                                                  "historical_edge", "combined"}
    clv = config["ablations"]["clv_only"]
    assert clv == sorted(w for w, v in screened.items() if v.get("clv_only")
                         and v["tier"] in ("T1", "T2", "T3"))
    # The CLV-only wallets are polled hourly and, once frozen, protected.
    ablation = dict(config, ablations={"rules": {}, "clv_only": ["0x" + "9" * 40]})
    assert "0x" + "9" * 40 in members(ablation)["wallets"]


def test_the_newest_generation_scored_before_the_cutoff_is_used(
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gen2 is scored a day after the cutoff, as when the daily score lands between
    # the freeze time and the freeze cycle. It would carry post-cutoff data, so the
    # freeze uses gen1 instead of refusing every cycle from then on.
    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Later:
            return cls.fromtimestamp((CUTOFF + timedelta(days=1)).timestamp(), tz)

    monkeypatch.setattr(snapshot, "datetime", Later)
    snapshot.score_snapshot(data, data / "score-snapshots", "gen2",
                            score_version="forecast-v5")
    monkeypatch.undo()
    assert snapshot.load_current(data / "score-snapshots")["run_id"] == "gen2"
    at_cutoff = build(data, cutoff=CUTOFF, discovery={})["score_generation"]
    assert at_cutoff["run_id"] == "gen1" and at_cutoff["started_at"] < CUTOFF.isoformat()
    later = build(data, cutoff=CUTOFF + timedelta(days=2), discovery={})
    assert later["score_generation"]["run_id"] == "gen2"
    with pytest.raises(ValueError, match="no score generation started"):
        build(data, cutoff=datetime(2020, 1, 1, tzinfo=UTC), discovery={})


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


# ── The pilot stage (build step 4) ───────────────────────────────────────────

def test_members_are_polled_first_and_the_rest_rotates(tmp_path: Path) -> None:
    from marketsignalos_polymarket.runner import _batch_with_priority, _build_stores

    stores = _build_stores(tmp_path)
    batch = _batch_with_priority({"0xa", "0xb", "0xc", "0xd"}, frozenset({"0xD"}),
                                 stores=stores, batch_size=2)
    assert batch == ["0xd", "0xa"]  # the member, then the oldest-polled of the rest
    # Members are always polled, even if they alone exceed the batch.
    assert _batch_with_priority({"0xa"}, frozenset({"0xc", "0xb", "0xd"}), stores=stores,
                                batch_size=2) == ["0xb", "0xc", "0xd"]


def test_the_stage_is_provisional_until_the_freeze_then_frozen_once(data: Path) -> None:
    before = CUTOFF - timedelta(days=7)
    provisional = cohort_v1.run(data, now=before, freeze_at=CUTOFF, discovery={})
    assert provisional["mode"] == "provisional"
    roster = json.loads((data / "cohort-v1" / "members.json").read_text())
    assert roster["mode"] == "provisional"
    assert cohort_v1.frozen_members(data) == frozenset()  # nothing protected yet

    frozen = cohort_v1.run(data, now=CUTOFF, freeze_at=CUTOFF, discovery={})
    assert frozen["mode"] == "frozen_now"
    config = json.loads((data / "cohort-v1" / "frozen-config.json").read_text())
    assert config["cutoff"] == CUTOFF.isoformat()
    assert config["window"]["opens"] == CUTOFF.isoformat()
    assert config["config_hash"] == config_hash(config) == frozen["config_hash"]
    assert json.loads((data / "cohort-v1" / "members.json").read_text())["mode"] == "frozen"

    # Later runs only verify; a tampered config is refused, never rewritten.
    later = cohort_v1.run(data, now=CUTOFF + timedelta(days=1), freeze_at=CUTOFF,
                          discovery={})
    assert later == {"status": "succeeded", "mode": "frozen",
                     "config_hash": config["config_hash"], "members": frozen["comparison"]
                     + len(config["tiers"]["T2"])}
    config["primary_tier"] = "T3"
    (data / "cohort-v1" / "frozen-config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="hash"):
        cohort_v1.run(data, now=CUTOFF + timedelta(days=2), freeze_at=CUTOFF, discovery={})


def test_the_window_comes_from_the_members_recent_events(data: Path) -> None:
    window = cohort_v1.power_window(data, [WALLET], CUTOFF)
    # Two buys in the 14 days before the cutoff, both in one event.
    assert window["events_last_14_days"] == 1
    assert window["days"] == 42 and window["under_powered"] is True
    assert cohort_v1.power_window(data, [], CUTOFF)["under_powered"] is True


def test_the_pilot_freezes_at_the_first_cycle_after_the_freeze_time(tmp_path: Path) -> None:
    from marketsignalos_polymarket import lean_pilot as pilot

    config = pilot.PilotConfig(cohort_v1_every_seconds=86400,
                               cohort_v1_freeze_at=CUTOFF.isoformat())
    before = pilot.plan_cycle(tmp_path, config, now=CUTOFF - timedelta(hours=1))
    assert "cohort_v1" in before["due"]  # never run: due on its interval anyway
    state = tmp_path / ".lean-pilot" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"schema_version": 1, "days": {}, "active": None,
                                 "stages": {"cohort_v1": {
                                     "last_attempt_at": (CUTOFF - timedelta(hours=2)).isoformat(),
                                     "last_status": "succeeded"}}}))
    assert "cohort_v1" not in pilot.plan_cycle(tmp_path, config,
                                               now=CUTOFF - timedelta(hours=1))["due"]
    assert "cohort_v1" in pilot.plan_cycle(tmp_path, config, now=CUTOFF)["due"]
    (tmp_path / "cohort-v1").mkdir()
    (tmp_path / "cohort-v1" / "frozen-config.json").write_text("{}")
    assert "cohort_v1" not in pilot.plan_cycle(tmp_path, config, now=CUTOFF)["due"]
    with pytest.raises(ValueError, match="timezone"):
        pilot.PilotConfig(cohort_v1_every_seconds=1, cohort_v1_freeze_at="2030-01-01T00:00")
    with pytest.raises(ValueError, match="enabled"):
        pilot.PilotConfig(cohort_v1_freeze_at=CUTOFF.isoformat())


def test_a_wallet_excluded_after_scoring_is_screened_out(data: Path) -> None:
    # 2026-10-07: the cohort stage excluded a provisional member after the score
    # generation that still listed it; it must leave the tiers, not stay a member.
    assert build(data, cutoff=CUTOFF, discovery={})["screened"][WALLET]["tier"] != "excluded"
    with (data / "excluded_wallets.txt").open("a", encoding="utf-8") as out:
        out.write(f"{WALLET}\t2026-10-07T08:11:00+00:00\tsystematic\n")
    config = build(data, cutoff=CUTOFF, discovery={})
    assert config["screened"][WALLET]["tier"] == "excluded"
    assert all(WALLET not in wallets for wallets in config["tiers"].values())


def test_collection_skips_excluded_provisional_members_but_never_frozen_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketsignalos_polymarket import cohort, runner
    from marketsignalos_polymarket import lean_pilot as pilot

    seen: dict[str, Any] = {}

    class Result:
        def to_dict(self) -> dict[str, Any]:
            return {"windows_succeeded": ["month"], "wallets_with_errors": 0}

    def fake_pipeline(**kwargs: Any) -> Result:
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    stage = tmp_path / "cohort-v1"
    stage.mkdir()
    (stage / "members.json").write_text(json.dumps({"wallets": ["0xkeep", "0xgone"]}))
    cohort.record_exclusions(tmp_path, {"0xgone": "systematic", "0xbot": "systematic"},
                             now=datetime(2026, 10, 7, 8, 11, tzinfo=UTC))
    result = pilot._execute_stage("collect", tmp_path, pilot.PilotConfig(), "r")
    assert seen["priority_wallets"] == {"0xkeep"}
    assert seen["exclude_wallets"] == {"0xgone", "0xbot"}
    assert result["cohort_v1_members_polled"] == 1
    # Once frozen, a member is protected (S2): polled first, never excluded.
    frozen = {"cohort_id": "cohort-v1", "config_hash": "h",
              "tiers": {"T2": ["0xgone"]}, "comparison": {"pairs": {"0xgone": "0xkeep"}}}
    (stage / "frozen-config.json").write_text(json.dumps(frozen))
    pilot._execute_stage("collect", tmp_path, pilot.PilotConfig(), "r")
    assert seen["priority_wallets"] == {"0xkeep", "0xgone"}
    assert seen["exclude_wallets"] == {"0xbot"}
