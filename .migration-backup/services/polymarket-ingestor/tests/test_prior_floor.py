from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import gate_attrition
from marketsignalos_polymarket import prior_floor as diagnostic
from marketsignalos_polymarket.bayesian_skill import (
    _MAX_SIGMA2_POP,
    _MIN_SIGMA2_POP,
    Bet,
    PosteriorFit,
    apply_recency_weights,
    effective_sample_size,
    fit_wallet_posterior,
    population_prior_from_fits,
    weak_prior_fit,
)
from marketsignalos_polymarket.enrichment_shadow import canonical_record
from marketsignalos_polymarket.models import PolymarketWalletBet, PolymarketWalletHydration
from marketsignalos_polymarket.skill_computation import (
    _apply_event_weights,
    _enrichment_from_rollup,
    _WalletRollup,
)


def spread(n: int = 40) -> list[diagnostic.WalletFit]:
    """Identified fits: between-wallet variance .16, sampling variance .05, so sigma2 = .11."""
    return [diagnostic.WalletFit(-.1 + (.4 if i % 2 else -.4), .05, 50.0, separated=False)
            for i in range(n)]


def favourite(bets: int, price: float) -> diagnostic.WalletFit:
    fit = diagnostic.wallet_fit([Bet(entry_price=price, won=True)] * bets)
    assert fit is not None and fit.separated
    return fit


def test_production_variant_is_the_production_estimator() -> None:
    bet_lists = [[Bet(entry_price=.5, won=i % k == 0) for i in range(n)]
                 for k, n in ((2, 10), (3, 30), (4, 60), (5, 9))]
    bet_lists.append([Bet(entry_price=.98, won=True)] * 10)
    posterior = [f for f in map(weak_prior_fit, bet_lists) if f is not None]
    wallets = [f for f in map(diagnostic.wallet_fit, bet_lists) if f is not None]
    assert [f.separated for f in wallets] == [False] * 4 + [True]
    for count in (4, 5):
        expected = population_prior_from_fits(posterior[:count])
        estimate = diagnostic.moment_estimate(wallets[:count])
        assert (estimate.mu, estimate.sigma2) == (expected.mu, expected.sigma2)
    fallback = population_prior_from_fits([])
    assert diagnostic.moment_estimate(wallets[:1]) == diagnostic.PriorEstimate(
        fallback.mu, fallback.sigma2, fallback.sigma2, 1)
    assert diagnostic.wallet_fit(bet_lists[0][:2]) is None


def test_dersimonian_laird_matches_hand_calculation() -> None:
    fits = [diagnostic.WalletFit(0.0, 1.0, 5.0, False), diagnostic.WalletFit(3.0, 2.0, 5.0, False)]
    # w = [1, .5]; fixed mean 1; Q = 3; scale = 1.5 - 1.25/1.5; tau2 = (3 - 1) / scale = 3.
    estimate = diagnostic.dersimonian_laird(fits)
    assert estimate.sigma2_raw == pytest.approx(3.0)
    assert estimate.sigma2 == pytest.approx(3.0)
    assert estimate.mu == pytest.approx(.6 / .45)
    tight = [diagnostic.WalletFit(0.0, 1.0, 5.0, False), diagnostic.WalletFit(.1, 1.0, 5.0, False)]
    below = diagnostic.dersimonian_laird(tight)
    assert below.sigma2_raw == pytest.approx(.005 - 1)
    assert below.sigma2 == _MIN_SIGMA2_POP


def test_paule_mandel_solves_the_generalized_q_equation() -> None:
    equal = [diagnostic.WalletFit(m, 1.0, 5.0, False) for m in (0.0, 2.0, 4.0)]
    assert diagnostic.paule_mandel(equal).sigma2_raw == pytest.approx(3.0, abs=1e-9)
    means, variances = (0.0, 3.0, -1.0, 2.0), (.5, 1.0, 2.0, .25)
    fits = [diagnostic.WalletFit(m, v, 5.0, False) for m, v in zip(means, variances, strict=True)]
    tau2 = diagnostic.paule_mandel(fits).sigma2_raw
    weights = [1 / (v + tau2) for v in variances]
    mu = sum(w * m for w, m in zip(weights, means, strict=True)) / sum(weights)
    q = sum(w * (m - mu) ** 2 for w, m in zip(weights, means, strict=True))
    assert q == pytest.approx(len(fits) - 1, abs=1e-9)
    same = [diagnostic.WalletFit(1.0, 1.0, 5.0, False)] * 3
    assert (diagnostic.paule_mandel(same).sigma2_raw, diagnostic.paule_mandel(same).sigma2) == (
        0.0, _MIN_SIGMA2_POP)


def test_separated_low_information_fits_pin_the_moment_estimator_to_its_floor() -> None:
    clean = spread()
    assert diagnostic.moment_estimate(clean).sigma2 == pytest.approx(.11)
    # Ten 98-cent wins: weak-prior MAP ~2.2 but posterior variance ~31, near the weak prior's.
    polluted = clean + [favourite(10, .98)] * 3
    production = diagnostic.moment_estimate(polluted)
    assert production.sigma2_raw < 0
    assert production.sigma2 == _MIN_SIGMA2_POP
    assert diagnostic.min_ess_moments(polluted).sigma2 == pytest.approx(.11)
    # ESS measures events, not information: thirty 99.9-cent wins survive the ESS cut.
    heavy = clean + [favourite(30, .999)] * 3
    assert diagnostic.min_ess_moments(heavy).sigma2 == _MIN_SIGMA2_POP
    for fits in (polluted, heavy):
        for estimator in (diagnostic.dersimonian_laird, diagnostic.paule_mandel):
            assert estimator(fits).sigma2 == pytest.approx(.11, abs=.02)
    # The +-20 clamp is unreachable under the N(0, 100) weak prior, so (c) changes nothing here.
    assert diagnostic.clamp_both_terms(polluted) == production
    decomposition = diagnostic.moment_decomposition(polluted)
    assert decomposition["separated_fits"] == 3
    assert decomposition["clamped_fits"] == 0
    assert decomposition["separated_net_contribution"] < 0
    assert decomposition["raw_sigma2_without_separated_fits"] == pytest.approx(.11)


def test_clamp_both_terms_drops_a_clamped_fit_from_both_moments() -> None:
    clean = spread()
    fits = [*clean, diagnostic.WalletFit(25.0, 50.0, 50.0, True)]
    assert diagnostic.moment_estimate(fits).sigma2 == _MAX_SIGMA2_POP
    assert diagnostic.clamp_both_terms(fits) == diagnostic.moment_estimate(clean)


def test_estimators_reject_degenerate_variances() -> None:
    for estimator in (diagnostic.moment_estimate, diagnostic.dersimonian_laird,
                      diagnostic.paule_mandel):
        with pytest.raises(ValueError, match="positive finite variance"):
            estimator([*spread(2), diagnostic.WalletFit(0.0, 0.0, 5.0, False)])


def test_prior_invariance_bound_counts_only_prior_dependent_failures() -> None:
    analysis = {"wallets": 7, "failure_patterns": [
        {"gates": [8, 9, 12], "wallets": 1}, {"gates": [6, 8], "wallets": 2},
        {"gates": [8, 9, 13], "wallets": 1}, {"gates": [10, 13], "wallets": 1},
        {"gates": [7, 8], "wallets": 1}, {"gates": [1, 13], "wallets": 1},
    ]}
    bound = diagnostic.prior_invariance_bound(analysis)
    assert bound["max_qualifiers_any_prior"] == {
        "production_gates": 1, "data_gates_suspended": 3, "clv_gate_suspended": 2,
        "data_and_clv_gates_suspended": 5,
    }
    assert bound["trusted_ess_20_wallets"] == 3
    assert bound["trusted_ess_20_failing_clv_gate"] == 2
    assert bound["trusted_ess_20_prior_independent_failures"] == [
        {"gates": [], "wallets": 1}, {"gates": [10, 13], "wallets": 1},
        {"gates": [13], "wallets": 1},
    ]
    # Wallets failing 8, 9 and 12 together count once as prior-driven, not three times.
    assert bound["prior_independent_model_economic_failure_histogram"] == {
        "0": 3, "1": 3, "2": 1,
    }
    for broken in ({**analysis, "wallets": 8},
                   {**analysis, "failure_patterns": [{"gates": [14], "wallets": 7}]}):
        with pytest.raises(ValueError):
            diagnostic.prior_invariance_bound(broken)


def test_committed_stage0_medians_sit_exactly_on_the_floor_prior_point() -> None:
    # Medians from docs/benchmarks/2026-09-10-gate-attrition.json (trusted cohort).
    assert diagnostic.prior_point(-.088177, _MIN_SIGMA2_POP) == {
        "forecast_skill_likelihood": .188951, "forecast_edge_lower_bound": -.252662,
        "recent_edge_mean": -.088177,
    }
    low, high = diagnostic.sigma2_interval(-.088177, -.252662)
    assert low <= _MIN_SIGMA2_POP <= high
    assert high - low < 1e-6


def distributions(mu: float, sigma2: float) -> dict[str, Any]:
    point = diagnostic.prior_point(mu, sigma2)
    fields = {metric: {"p10": value - .1, "median": value, "p90": value + .1}
              for metric, value in point.items()}
    return {"rounded_metric_distributions": {
        "all_wallets": {metric: {**q, "median": q["median"] + .01} for metric, q in fields.items()},
        "trusted_wallets": fields,
    }}


def test_prior_recovery_distinguishes_a_floor_prior_from_a_fitted_one() -> None:
    at_floor = diagnostic.recover_prior(distributions(-.088177, _MIN_SIGMA2_POP))
    assert at_floor["floor_inside_interval"] is True
    assert len(at_floor["exact_quantile_matches"]) == 3
    fitted = diagnostic.recover_prior(distributions(-.088177, .04))
    assert fitted["floor_inside_interval"] is False
    assert len(fitted["exact_quantile_matches"]) == 1  # only the proposed mean itself


def test_prior_gate_reasons_match_the_production_scorer() -> None:
    wallet = "0x" + "cd" * 20
    rollup = _WalletRollup(
        wallet=wallet, name="", pseudonym="", bets=[Bet(entry_price=.5, won=True)] * 25,
        wins=25, losses=0, total_pnl_usdc=1000, total_volume_usdc=5000,
        resolved_volume_usdc=5000, trade_count=25, last_activity_ts=1000,
        position_sizes=[100] * 25, records=[],
    )
    hydration = PolymarketWalletHydration(
        proxy_wallet=wallet, activity_history_complete=True, positions_complete=True,
        closed_positions_complete=True, economic_all_time_complete=True,
        economic_month_complete=True, metadata_condition_count=25, metadata_covered_count=25,
        metadata_coverage=1, all_time_pnl_usdc=1000, all_time_volume_usdc=5000, pnl_30d_usdc=100,
    )
    fit = PosteriorFit(edge_mean=.5, edge_var=.01, posterior_skill=.99,
                       edge_lower_bound=.3, edge_z=5)
    for skill in (.79999999, .8):
        for lower_bound in (-1e-8, 0.0, 1e-8):
            for recent_edge in (-1e-9, 0.0):
                for recent_ess in (4.9999, 5.0):
                    lifetime = replace(fit, posterior_skill=skill, edge_lower_bound=lower_bound)
                    recent = replace(fit, edge_mean=recent_edge)
                    row = asdict(_enrichment_from_rollup(
                        rollup, lifetime, ess=25, recent_fit=recent, recent_ess=recent_ess,
                        clv_stats=(.05, .02, 15), hydration=hydration,
                    ))
                    expected = [r for r in row["tailability_reasons"]
                                if r in diagnostic.PRIOR_REASONS]
                    assert diagnostic.prior_gate_reasons(
                        lifetime, recent, recent_sample_ok=recent_ess >= 5) == expected


# ── End to end on a production-scored fixture ────────────────────────────────

T0 = 1_750_000_000


def fixture_positions() -> dict[str, list[dict[str, Any]]]:
    def leg(event: str, price: float, won: bool | None, size: float, day: int) -> dict[str, Any]:
        return {"event": event, "price": price, "won": won, "size": size, "ts": T0 + day * 86_400}

    return {
        f"0x{1:040x}": [leg(f"a{i}", .5, i % 3 != 0, 10 + i, i * 7) for i in range(30)],
        f"0x{2:040x}": [leg(f"b{i // 2}", .6, i % 2 == 0, 5 + 3 * i, 150 + i) for i in range(26)]
        + [leg("b-exit", .4, None, 0.0, 170)],
        f"0x{3:040x}": [leg(f"c{i}", .98, True, 50, 200 + i) for i in range(5)],
        f"0x{4:040x}": [leg("d-exit", .3, None, 0.0, 10)],
        f"0x{5:040x}": [leg(f"e{i}", .45, bool(i), 20, 30 + i) for i in range(2)],
    }


def write_fixture(tmp_path: Path) -> dict[str, Path]:
    positions = fixture_positions()
    bets = {
        wallet: _apply_event_weights([
            Bet(entry_price=p["price"], won=p["won"], event_slug=p["event"],
                cost_usdc=p["size"] * p["price"], ts=p["ts"])
            for p in legs if p["won"] is not None
        ]) for wallet, legs in positions.items()
    }
    prior = population_prior_from_fits(
        [f for f in map(weak_prior_fit, bets.values()) if f is not None])
    now_ts = max(b.ts for items in bets.values() for b in items)
    scores, records = [], []
    for wallet, items in bets.items():
        recent = apply_recency_weights(items, now_ts=now_ts)
        wins = sum(b.won for b in items)
        rollup = _WalletRollup(
            wallet=wallet, name="", pseudonym="", bets=items, wins=wins,
            losses=len(items) - wins, total_pnl_usdc=10, total_volume_usdc=100,
            resolved_volume_usdc=100, trade_count=len(items), last_activity_ts=now_ts,
            position_sizes=[b.cost_usdc for b in items], records=[],
        )
        hydration = PolymarketWalletHydration(
            proxy_wallet=wallet, activity_history_complete=True, positions_complete=True,
            closed_positions_complete=True, economic_all_time_complete=True,
            economic_month_complete=True, metadata_condition_count=1, metadata_covered_count=1,
            metadata_coverage=1, all_time_pnl_usdc=10, all_time_volume_usdc=100, pnl_30d_usdc=1,
        )
        scores.append(canonical_record(_enrichment_from_rollup(
            rollup, fit_wallet_posterior(items, mu_prior=prior.mu, sigma2_prior=prior.sigma2),
            ess=effective_sample_size(items),
            recent_fit=fit_wallet_posterior(recent, mu_prior=prior.mu, sigma2_prior=prior.sigma2),
            recent_ess=effective_sample_size(recent), clv_stats=(.01, .001, 12),
            hydration=hydration,
        )))
        for p in positions[wallet]:
            status = "exited" if p["won"] is None else "won" if p["won"] else "lost"
            records.append(canonical_record(PolymarketWalletBet(
                proxy_wallet=wallet, condition_id=p["event"], outcome_index=0, outcome="Yes",
                event_slug=p["event"], category="", title="", status=status,
                entry_price=p["price"], cost_usdc=round(p["size"] * p["price"], 2),
                net_size=p["size"], realized_pnl_usdc=0.0, total_pnl_usdc=0.0, clv=None,
                last_trade_ts=p["ts"],
            )))
    paths = {"snapshot": tmp_path / "enrichments.jsonl", "bets": tmp_path / "bets.jsonl",
             "benchmark": tmp_path / "shadow.json", "attrition": tmp_path / "attrition.json"}
    paths["snapshot"].write_text("".join(scores), encoding="utf-8", newline="\n")
    paths["bets"].write_text("".join(records), encoding="utf-8", newline="\n")
    outputs = {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
               for name, raw in (("enrichments", paths["snapshot"].read_bytes()),
                                 ("bets", paths["bets"].read_bytes()))}
    tailable = sum('"tailability_status":"tailable"' in s for s in scores)
    paths["benchmark"].write_text(json.dumps({"status": "passed", "measurements": [
        {"operation": engine, "result": {"wallets": len(scores), "tailable_wallets": tailable,
                                         "outputs": outputs}}
        for engine in ("jsonl", "parquet")
    ]}), encoding="utf-8")
    stage0 = gate_attrition.build_report(paths["snapshot"], paths["benchmark"])
    paths["attrition"].write_text(json.dumps(stage0), encoding="utf-8")
    return paths


def test_refit_reproduces_production_scores_before_comparing_estimators(tmp_path: Path) -> None:
    paths = write_fixture(tmp_path)
    report = diagnostic.build_report(paths["attrition"], snapshot=paths["snapshot"],
                                     bets=paths["bets"], benchmark=paths["benchmark"])
    refit = report["refit"]
    assert report["refit_status"] == "completed"
    assert refit["weak_prior_fits"] == 3
    reproduction = refit["reproduction"]
    assert all(item["outside_rounding_interval"] == 0 for item in reproduction["fields"].values())
    assert reproduction["gate_decision_disagreements"] == {"8": 0, "9": 0, "12": 0}
    assert reproduction["waterfall_matches_stored"] is True
    assert reproduction["failure_patterns_match_stored"] is True
    production = refit["variants"][0]
    rows = [json.loads(line) for line in paths["snapshot"].read_text().splitlines()]
    assert refit["stored_prior_mean_from_no_evidence_wallets"] == pytest.approx(
        production["mu"], abs=5e-7)
    assert next(r for r in rows if r["resolved_trades"] == 0)["forecast_edge_mean"] == round(
        production["mu"], 6)
    assert [v["variant"] for v in refit["variants"]] == list(diagnostic.ESTIMATORS)
    stored = json.loads(paths["attrition"].read_text())["analysis"]
    assert production["counterfactuals"] == stored["counterfactuals"]
    assert refit["moment_decomposition"]["separated_fits"] == 1
    assert report["provenance"]["bets"]["sha256"] == hashlib.sha256(
        paths["bets"].read_bytes()).hexdigest()
    assert "Reproduction check" in diagnostic.render_markdown(report)


def test_refit_rejects_inputs_that_are_not_the_verified_shadow_outputs(tmp_path: Path) -> None:
    paths = write_fixture(tmp_path)
    with pytest.raises(ValueError, match="together"):
        diagnostic.build_report(paths["attrition"], snapshot=paths["snapshot"])
    with paths["bets"].open("a", encoding="utf-8") as handle:
        handle.write(canonical_record(PolymarketWalletBet(
            proxy_wallet=f"0x{1:040x}", condition_id="late", outcome_index=0, outcome="Yes",
            event_slug="late", category="", title="", status="open", entry_price=.5,
            cost_usdc=1, net_size=2, realized_pnl_usdc=0, total_pnl_usdc=0, clv=None,
            last_trade_ts=T0,
        )))
    with pytest.raises(ValueError, match="bets hash/size"):
        diagnostic.build_report(paths["attrition"], snapshot=paths["snapshot"],
                                bets=paths["bets"], benchmark=paths["benchmark"])
    stranger = json.loads(paths["bets"].read_text().splitlines()[0])
    stranger["proxy_wallet"] = f"0x{99:040x}"
    paths["bets"].write_text(json.dumps(stranger) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing from the score snapshot"):
        diagnostic.load_bets(paths["bets"], {f"0x{1:040x}"})
    other = json.loads(paths["attrition"].read_text())
    other["provenance"]["snapshot"]["sha256"] = "0" * 64
    paths["attrition"].write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(ValueError, match="not the one the attrition receipt analyzed"):
        diagnostic.build_report(paths["attrition"], snapshot=paths["snapshot"],
                                bets=paths["bets"], benchmark=paths["benchmark"])


def test_cli_runs_without_local_data_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    output = tmp_path / "reports" / "prior-floor.json"
    monkeypatch.setattr("sys.argv", ["prior_floor", "--attrition", str(paths["attrition"]),
                                   "--output", str(output)])
    diagnostic.main()
    report = json.loads(output.read_text())
    assert report["refit_status"] == "pending_local_inputs"
    assert "Pending local inputs" in output.with_suffix(".md").read_text()
    with pytest.raises(SystemExit):
        diagnostic.main()
    full = tmp_path / "reports" / "prior-floor-refit.json"
    monkeypatch.setattr("sys.argv", [
        "prior_floor", "--attrition", str(paths["attrition"]), "--snapshot",
        str(paths["snapshot"]), "--bets", str(paths["bets"]), "--benchmark",
        str(paths["benchmark"]), "--output", str(full),
    ])
    diagnostic.main()
    assert json.loads(full.read_text())["refit_status"] == "completed"
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_reproduction_check_detects_a_reconstruction_error(tmp_path: Path) -> None:
    paths = write_fixture(tmp_path)
    rows = [json.loads(line) for line in paths["snapshot"].read_text().splitlines()]
    bets, _ = diagnostic.load_bets(paths["bets"], {r["proxy_wallet"] for r in rows})
    stage0 = json.loads(paths["attrition"].read_text())["analysis"]
    wallet = f"0x{1:040x}"
    bets[wallet] = [replace(bets[wallet][0], won=not bets[wallet][0].won), *bets[wallet][1:]]
    fields = diagnostic.refit_variants(rows, bets, stage0)["reproduction"]["fields"]
    assert fields["forecast_edge_mean"]["outside_rounding_interval"] >= 1
    assert fields["independent_settled_events"]["outside_rounding_interval"] == 0
