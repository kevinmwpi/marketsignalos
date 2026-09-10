from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import gate_attrition as diagnostic
from marketsignalos_polymarket.bayesian_skill import Bet, PosteriorFit
from marketsignalos_polymarket.models import PolymarketWalletHydration
from marketsignalos_polymarket.skill_computation import _enrichment_from_rollup, _WalletRollup


def score(n: int = 1, reasons: tuple[str, ...] = (), **metrics: float) -> dict[str, Any]:
    data = [r for r in reasons if r in diagnostic.DATA_REASONS]
    return {
        "proxy_wallet": f"0x{n:040x}", "score_version": "forecast-v4",
        "data_quality_reasons": data, "data_quality_status": "untrusted" if data else "trusted",
        "tailability_reasons": list(reasons), "tailability_status": "blocked" if reasons else "tailable",
        "economic_qualified": not any(r in diagnostic.GATES[9].reasons for r in reasons),
        "independent_settled_events": 100, "forecast_skill_likelihood": .95,
        "forecast_edge_lower_bound": .1, "all_time_pnl_usdc": 100,
        "all_time_roi": .1, "pnl_30d_usdc": 10, "recent_independent_events": 20,
        "recent_edge_mean": .05, "clv_sample_size": 20, "clv_lower_bound": .01,
        **metrics,
    }


def test_waterfall_overlap_and_three_counterfactuals() -> None:
    rows = [
        score(), score(2, ("incomplete market metadata",)),
        score(3, ("incomplete market metadata", "forecast confidence below 80%",
                  "conservative edge is not positive"),
              forecast_skill_likelihood=.4, forecast_edge_lower_bound=-.2),
        score(4, ("fewer than 10 closing-line observations",), clv_sample_size=7),
        score(5, ("fewer than 10 closing-line observations",), clv_sample_size=7, clv_lower_bound=-.1),
        score(6, ("incomplete activity history",)),
        score(7, ("fewer than 5 recent independent settled events",),
              recent_independent_events=2, recent_edge_mean=-.2),
        score(8, ("closing-line value not confidently positive",), clv_lower_bound=-.1),
    ]
    result = diagnostic.analyze(rows)
    assert result["tailable_wallets"] == 1
    assert result["counterfactuals"] == {
        "data_gates_suspended": 3, "metadata_gate_suspended": 2,
        "clv_minimum_five": {"qualified_min": 2, "qualified_max": 2,
                             "ambiguous_eligible_wallets": 0},
    }
    assert result["failure_groups"] == {
        "coverage_only": 2, "model_or_economics_only": 4, "both": 1, "neither": 1,
    }
    waterfall = result["waterfall"]
    assert [g["remaining"] for g in waterfall] == [7, 7, 7, 7, 7, 5, 5, 5, 5, 5, 4, 4, 1]
    assert waterfall[5]["isolated_failures"] == 2
    assert waterfall[11]["isolated_failures"] == 0
    assert waterfall[11]["not_evaluated"] == 1
    assert sum(g["removed"] for g in waterfall) + result["tailable_wallets"] == len(rows)
    assert diagnostic.analyze(list(reversed(rows))) == result


@pytest.mark.parametrize("metrics", [
    {"clv_sample_size": 5}, {"clv_sample_size": 7, "clv_lower_bound": 0},
])
def test_rounding_ambiguity_is_reported_not_guessed(metrics: dict[str, float]) -> None:
    result = diagnostic.analyze([
        score(), score(2, ("fewer than 10 closing-line observations",), **metrics),
    ])
    assert result["counterfactuals"]["clv_minimum_five"] == {
        "qualified_min": 1, "qualified_max": 2, "ambiguous_eligible_wallets": 1,
    }


def test_pre_rounding_decisions_win_at_boundary_and_other_gates_remain() -> None:
    result = diagnostic.analyze([
        score(forecast_skill_likelihood=.8, forecast_edge_lower_bound=0, clv_lower_bound=0),
        score(2, ("incomplete market metadata", "fewer than 10 closing-line observations"),
              clv_sample_size=5, clv_lower_bound=0),
    ])
    assert result["tailable_wallets"] == 1
    assert result["counterfactuals"]["clv_minimum_five"] == {
        "qualified_min": 1, "qualified_max": 1, "ambiguous_eligible_wallets": 0,
    }


@pytest.mark.parametrize("patch", [
    {"score_version": "forecast-v5"}, {"tailability_reasons": ["new unknown gate"]},
    {"tailability_status": "blocked"}, {"data_quality_status": "untrusted"},
    {"economic_qualified": False}, {"economic_qualified": 1},
    {"clv_sample_size": None}, {"clv_sample_size": -1}, {"clv_sample_size": float("nan")},
    {"clv_lower_bound": float("inf")}, {"forecast_skill_likelihood": True},
    {"forecast_skill_likelihood": .2}, {"all_time_roi": -.1},
    {"proxy_wallet": "not-a-wallet"}, {"data_quality_reasons": ["incomplete market metadata"]},
])
def test_invalid_or_contradictory_scores_fail_closed(patch: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        diagnostic.analyze([{**score(), **patch}])


def test_empty_and_duplicate_wallets_are_rejected() -> None:
    with pytest.raises(ValueError, match="no wallets"):
        diagnostic.analyze([])
    with pytest.raises(ValueError, match="Duplicate wallet"):
        diagnostic.analyze([score(171), {**score(171), "proxy_wallet": "0x" + "0" * 38 + "AB"}])


def test_impossible_conditional_reasons_are_rejected() -> None:
    with pytest.raises(ValueError, match="Gate 12"):
        diagnostic.analyze([score(reasons=("fewer than 5 recent independent settled events",
                                          "recent forecast edge is negative"),
                                  recent_independent_events=1, recent_edge_mean=-.2)])
    with pytest.raises(ValueError, match="CLV confidence"):
        diagnostic.analyze([score(reasons=("fewer than 10 closing-line observations",
                                          "closing-line value not confidently positive"),
                                  clv_sample_size=1, clv_lower_bound=-.2)])


def test_real_scorer_reason_mapping_including_rounding_and_conditional_gates() -> None:
    wallet = "0x" + "ab" * 20
    rollup = _WalletRollup(
        wallet=wallet, name="", pseudonym="", bets=[Bet(entry_price=.5, won=True)] * 25,
        wins=25, losses=0, total_pnl_usdc=1000, total_volume_usdc=5000,
        resolved_volume_usdc=5000, trade_count=25, last_activity_ts=1000,
        position_sizes=[100] * 25, records=[],
    )
    fit = PosteriorFit(edge_mean=.5, edge_var=.01, posterior_skill=.99,
                       edge_lower_bound=.3, edge_z=5)
    hydration = PolymarketWalletHydration(
        proxy_wallet=wallet, activity_history_complete=True, positions_complete=True,
        closed_positions_complete=True, economic_all_time_complete=True, economic_month_complete=True,
        metadata_condition_count=25, metadata_covered_count=25, metadata_coverage=1,
        all_time_pnl_usdc=1000, all_time_volume_usdc=5000, pnl_30d_usdc=100,
    )
    # True production decisions at and around serialized boundaries.
    cases: list[dict[str, Any]] = [
        {}, {"ess": 19.999999}, {"fit": replace(fit, posterior_skill=.79999999)},
        {"fit": replace(fit, edge_lower_bound=.00000001)},
        {"fit": replace(fit, edge_lower_bound=-.00000001)},
        {"recent_ess": 1, "recent_fit": replace(fit, edge_mean=-.1)},
        {"recent_fit": replace(fit, edge_mean=-.1)},
        {"clv_stats": (.1, .01, 4.999999)}, {"clv_stats": (.1, -.01, 15)},
        {"clv_stats": (.1, .00000001, 15)},
        {"hydration": replace(hydration, all_time_pnl_usdc=-1, pnl_30d_usdc=-1)},
        {"hydration": replace(hydration, activity_history_complete=False, positions_complete=False,
                               closed_positions_complete=False, economic_all_time_complete=False,
                               economic_month_complete=False, metadata_coverage=.5)},
    ]
    for change in cases:
        args = {"fit": fit, "ess": 25, "recent_fit": fit, "recent_ess": 25,
                "clv_stats": (.05, .02, 15), "hydration": hydration, **change}
        row = asdict(_enrichment_from_rollup(rollup, **args))
        result = diagnostic.analyze([row])
        assert result["tailable_wallets"] == int(row["tailability_status"] == "tailable")
        assert sum(g["isolated_failures"] for g in result["waterfall"]) > 0 or not row["tailability_reasons"]


def evidence(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "scores.jsonl"
    snapshot.write_text(json.dumps(score()) + "\n", encoding="utf-8")
    raw = snapshot.read_bytes()
    receipt = {"status": "passed", "measurements": [
        {"operation": engine, "result": {
            "wallets": 1, "tailable_wallets": 1,
            "outputs": {"enrichments": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}},
        }} for engine in ("jsonl", "parquet")
    ]}
    benchmark = tmp_path / "shadow.json"
    benchmark.write_text(json.dumps(receipt), encoding="utf-8")
    return snapshot, benchmark


def test_provenance_requires_both_engines_and_input_hashes(tmp_path: Path) -> None:
    snapshot, benchmark = evidence(tmp_path)
    report = diagnostic.build_report(snapshot, benchmark)
    assert report["analysis"]["wallets"] == 1
    assert report["provenance"]["snapshot"]["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    changed = json.loads(benchmark.read_text())
    changed["measurements"].pop()
    benchmark.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="parquet shadow"):
        diagnostic.build_report(snapshot, benchmark)
    snapshot.write_text(json.dumps(score(2)) + "\n")
    with pytest.raises(ValueError, match="hash/size"):
        diagnostic.build_report(snapshot, benchmark)


def test_cli_is_read_only_and_does_not_overwrite_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot, benchmark = evidence(tmp_path)
    before = snapshot.read_bytes(), benchmark.read_bytes()
    output = tmp_path / "reports" / "attrition.json"
    monkeypatch.setattr("sys.argv", ["gate_attrition", "--snapshot", str(snapshot),
                                   "--benchmark", str(benchmark), "--output", str(output)])
    diagnostic.main()
    assert (snapshot.read_bytes(), benchmark.read_bytes()) == before
    assert "Diagnostic counterfactuals" in output.with_suffix(".md").read_text()
    with pytest.raises(SystemExit):
        diagnostic.main()
