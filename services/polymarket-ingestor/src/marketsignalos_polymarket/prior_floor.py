"""Read-only diagnostic: is Stage 0's empty feed an artifact of the prior's variance floor?

Two parts. The bound needs only the committed Stage 0 receipt: gates 8, 9 and 12 are
the only gates whose inputs depend on the empirical-Bayes population prior, so a wallet
can qualify under *some* prior only if every gate it failed is one of those three.
The refit needs the frozen score snapshot and the bets export from the same enrichment
shadow run: it re-estimates the prior with alternative estimators, refits every
wallet's lifetime and recent posteriors, and re-runs the Stage 0 waterfall with
production gates unchanged.

No production code, threshold, default, or store is touched. The estimator variants
exist here only as diagnostics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import gate_attrition
from .bayesian_skill import (
    _EDGE_CLAMP,
    _MAX_SIGMA2_POP,
    _MIN_SIGMA2_POP,
    _NORM_05,
    Bet,
    PopulationPrior,
    PosteriorFit,
    apply_recency_weights,
    effective_sample_size,
    fit_wallet_posterior,
    population_prior_from_fits,
    weak_prior_fit,
)
from .skill_computation import _apply_event_weights

PRIOR_DEPENDENT_GATES = frozenset({8, 9, 12})
DATA_GATES = frozenset(range(1, 7))
MODEL_ECONOMIC_GATES = frozenset(range(7, 14))
# Variant (a) reuses gate 7's evidence threshold.
MIN_ESS = 20.0
# Production decision thresholds in `_enrichment_from_rollup`; a parity test pins them.
MIN_POSTERIOR_SKILL = 0.80
GATE_8_REASON = gate_attrition.GATES[7].reasons[0]
GATE_9_REASON = gate_attrition.GATES[8].reasons[0]
GATE_11_REASON = gate_attrition.GATES[10].reasons[0]
GATE_12_REASON = gate_attrition.GATES[11].reasons[0]
PRIOR_REASONS = frozenset({GATE_8_REASON, GATE_9_REASON, GATE_12_REASON})
REASON_ORDER = {
    reason: index
    for index, reason in enumerate(r for gate in gate_attrition.GATES for r in gate.reasons)
}
REFIT_FIELDS = (
    "forecast_skill_likelihood", "forecast_edge_mean", "forecast_edge_lower_bound",
    "recent_edge_mean",
)
EVIDENCE_FIELDS = ("independent_settled_events", "recent_independent_events")
_WALLET = re.compile(r"0x[0-9a-fA-F]{40}")
LIMITATIONS = [
    ("The bound is exact for the stored decisions. Gates 7 and 11 sum event weights, gate 10 "
     "reads hydration economics, gate 13 reads closing-line value, and gates 1-6 read hydration, "
     "so none of them depends on the population prior."),
    ("The refit rebuilds resolved bets from the bets export. Its entry price is the buys-only "
     "VWAP and its cost is net size times that price; both equal production's cost-basis inputs "
     "unless a position was bought again after a sell. The production-estimator reproduction "
     "check measures the effect before any variant is read."),
    ("Variants are diagnostics, not proposed production estimators. Each applied variance keeps "
     "production's floor and cap, so the gates see what production would have seen."),
    ("Historical, selected cohort. Qualifier counts are not prospective, fee-adjusted evidence "
     "that copying any wallet would be profitable."),
]


# ── Prior estimators ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WalletFit:
    """One wallet's weak-prior fit: the input every prior estimator shares."""

    edge_mean: float
    edge_var: float
    ess: float
    separated: bool  # every resolved bet won or every one lost; no finite MLE exists


@dataclass(frozen=True, slots=True)
class PriorEstimate:
    mu: float
    sigma2_raw: float  # before production's floor and cap; may be negative
    sigma2: float      # applied exactly as production applies its own estimate
    fits_used: int


def wallet_fit(bets: list[Bet]) -> WalletFit | None:
    """The weak-prior fit production feeds its population prior (None below 3 bets)."""
    fit = weak_prior_fit(bets)
    if fit is None:
        return None
    wins = sum(b.won for b in bets)
    return WalletFit(fit.edge_mean, fit.edge_var, effective_sample_size(bets),
                     separated=wins in (0, len(bets)))


def _clamped(fit: WalletFit) -> float:
    return min(_EDGE_CLAMP, max(-_EDGE_CLAMP, fit.edge_mean))


def _bounded(sigma2: float) -> float:
    return min(max(_MIN_SIGMA2_POP, sigma2), _MAX_SIGMA2_POP)


def _fallback(fits: list[WalletFit]) -> PriorEstimate:
    prior = population_prior_from_fits([])
    return PriorEstimate(prior.mu, prior.sigma2, prior.sigma2, len(fits))


def _checked(fits: list[WalletFit]) -> list[WalletFit]:
    for fit in fits:
        if not (math.isfinite(fit.edge_mean) and math.isfinite(fit.edge_var)) or fit.edge_var <= 0:
            raise ValueError("Weak-prior fits need a finite mean and positive finite variance")
    return fits


def moment_estimate(fits: list[WalletFit]) -> PriorEstimate:
    """Production's method of moments, in production's operation order."""
    if len(_checked(fits)) < 2:
        return _fallback(fits)
    means = [_clamped(f) for f in fits]
    mu = sum(means) / len(means)
    total_var = sum((m - mu) ** 2 for m in means) / len(means)
    within_var = sum(f.edge_var for f in fits) / len(fits)
    raw = total_var - within_var
    return PriorEstimate(mu, raw, _bounded(raw), len(fits))


def min_ess_moments(fits: list[WalletFit]) -> PriorEstimate:
    """Variant (a): the same moments over wallets with at least gate 7's evidence."""
    return moment_estimate([f for f in fits if f.ess >= MIN_ESS])


def clamp_both_terms(fits: list[WalletFit]) -> PriorEstimate:
    """Variant (c): a fit clamped out of the between-wallet term leaves the within term too."""
    return moment_estimate([f for f in fits if abs(f.edge_mean) < _EDGE_CLAMP])


def _random_effects_mu(means: list[float], variances: list[float], tau2: float) -> float:
    weights = [1.0 / (v + tau2) for v in variances]
    return sum(w * m for w, m in zip(weights, means, strict=True)) / sum(weights)


def dersimonian_laird(fits: list[WalletFit]) -> PriorEstimate:
    """Variant (b): precision-weighted moments. Raw tau^2 is reported before truncation."""
    if len(_checked(fits)) < 2:
        return _fallback(fits)
    means = [_clamped(f) for f in fits]
    variances = [f.edge_var for f in fits]
    weights = [1.0 / v for v in variances]
    total = sum(weights)
    fixed = sum(w * m for w, m in zip(weights, means, strict=True)) / total
    q = sum(w * (m - fixed) ** 2 for w, m in zip(weights, means, strict=True))
    scale = total - sum(w * w for w in weights) / total
    raw = (q - (len(fits) - 1)) / scale
    sigma2 = _bounded(raw)
    return PriorEstimate(_random_effects_mu(means, variances, sigma2), raw, sigma2, len(fits))


def paule_mandel(fits: list[WalletFit], *, iterations: int = 200) -> PriorEstimate:
    """Variant (b): tau^2 where the generalized Q statistic equals its expectation, k - 1.

    The statistic falls monotonically in tau^2, so bisection is exact to float precision.
    Paule-Mandel cannot go negative: an estimate of 0 means Q(0) is already at most k - 1.
    """
    if len(_checked(fits)) < 2:
        return _fallback(fits)
    means = [_clamped(f) for f in fits]
    variances = [f.edge_var for f in fits]

    def excess(tau2: float) -> float:
        mu = _random_effects_mu(means, variances, tau2)
        return sum((m - mu) ** 2 / (v + tau2)
                   for m, v in zip(means, variances, strict=True)) - (len(fits) - 1)

    raw = 0.0
    if excess(0.0) > 0.0:
        low, high = 0.0, max(sum(variances) / len(variances), 1e-12)
        for _ in range(iterations):
            if excess(high) <= 0.0:
                break
            low, high = high, high * 2.0
        else:
            raise ValueError("Paule-Mandel bracket did not close")
        for _ in range(iterations):
            middle = (low + high) / 2.0
            if middle in (low, high):
                break
            low, high = (middle, high) if excess(middle) > 0.0 else (low, middle)
        raw = (low + high) / 2.0
    sigma2 = _bounded(raw)
    return PriorEstimate(_random_effects_mu(means, variances, sigma2), raw, sigma2, len(fits))


Estimator = Callable[[list[WalletFit]], PriorEstimate]
ESTIMATORS: dict[str, tuple[str, Estimator]] = {
    "production": ("Production method of moments (reproduction baseline)", moment_estimate),
    "min_ess_20": ("(a) Method of moments, fits with ESS >= 20", min_ess_moments),
    "dersimonian_laird": ("(b) DerSimonian-Laird, precision-weighted", dersimonian_laird),
    "paule_mandel": ("(b) Paule-Mandel, precision-weighted", paule_mandel),
    "clamp_both_terms": ("(c) Method of moments, edge clamp on both terms", clamp_both_terms),
}


def moment_decomposition(fits: list[WalletFit]) -> dict[str, Any]:
    """Split production's variance estimate between separated and identified fits.

    A separated fit has no finite MLE. The weak prior pins its mean at a moderate value
    while its posterior variance approaches the weak prior's, so it adds far more to the
    subtracted within-wallet term than to the between-wallet spread.
    """
    if len(_checked(fits)) < 2:
        raise ValueError("Moment decomposition needs at least two fits")
    n = len(fits)
    estimate = moment_estimate(fits)
    separated = [f for f in fits if f.separated]
    within = sum(f.edge_var for f in fits) / n
    separated_within = sum(f.edge_var for f in separated) / n
    separated_between = sum((_clamped(f) - estimate.mu) ** 2 for f in separated) / n
    identified = moment_estimate([f for f in fits if not f.separated])
    return {
        "fits": n, "separated_fits": len(separated),
        "clamped_fits": sum(abs(f.edge_mean) >= _EDGE_CLAMP for f in fits),
        "between_wallet_variance": estimate.sigma2_raw + within,
        "within_wallet_variance": within,
        "raw_sigma2": estimate.sigma2_raw,
        "separated_share_of_within_variance": separated_within / within,
        "separated_net_contribution": separated_between - separated_within,
        "raw_sigma2_without_separated_fits": identified.sigma2_raw,
    }


# ── Data-free evidence from the committed Stage 0 receipt ────────────────────

def prior_point(mu: float, sigma2: float) -> dict[str, float]:
    """Rounded scores production reports for a wallet with no settled evidence."""
    fit = fit_wallet_posterior([], mu_prior=mu, sigma2_prior=sigma2)
    return {"forecast_skill_likelihood": round(fit.posterior_skill, 6),
            "forecast_edge_lower_bound": round(fit.edge_lower_bound, 6),
            "recent_edge_mean": round(fit.edge_mean, 6)}


def sigma2_interval(mu: float, lower_bound: float, *, decimals: int = 6) -> tuple[float, float]:
    """Prior variances consistent with a rounded prior-point mean and 5th percentile."""
    half = 0.5 * 10 ** -decimals
    low = ((mu - half) - (lower_bound + half)) / _NORM_05
    high = ((mu + half) - (lower_bound - half)) / _NORM_05
    if low <= 0:
        raise ValueError("A prior-point lower bound must sit below its mean")
    return low * low, high * high


def recover_prior(analysis: dict[str, Any]) -> dict[str, Any]:
    """Test the hypothesis that the stored scores were fitted under a floor-pinned prior.

    Wallets with no recent evidence report the prior mean as their recent edge, so the
    median recent edge proposes mu. The floor then predicts the prior point's skill and
    lower bound; exact matches among independently stored quantiles confirm it.
    """
    distributions = analysis["rounded_metric_distributions"]
    trusted = distributions["trusted_wallets"]
    mu = float(trusted["recent_edge_mean"]["median"])
    lower_bound = float(trusted["forecast_edge_lower_bound"]["median"])
    low, high = sigma2_interval(mu, lower_bound)
    predicted = prior_point(mu, _MIN_SIGMA2_POP)
    matches = [
        {"cohort": cohort, "metric": metric, "quantile": quantile}
        for cohort, fields in distributions.items()
        for metric, value in predicted.items()
        for quantile in ("p10", "median", "p90")
        if fields[metric][quantile] == value
    ]
    return {
        "proposed_mu": mu, "variance_floor": _MIN_SIGMA2_POP,
        "sigma2_interval_from_trusted_medians": [low, high],
        "floor_inside_interval": low <= _MIN_SIGMA2_POP <= high,
        "predicted_prior_point_at_floor": predicted, "exact_quantile_matches": matches,
    }


def _patterns(analysis: dict[str, Any]) -> list[tuple[frozenset[int], int]]:
    patterns = []
    for item in analysis["failure_patterns"]:
        gates, wallets = item["gates"], item["wallets"]
        if (not isinstance(wallets, int) or wallets < 1 or not isinstance(gates, list)
                or any(type(g) is not int or not 1 <= g <= 13 for g in gates)):
            raise ValueError("Invalid failure pattern in attrition receipt")
        patterns.append((frozenset(gates), wallets))
    if sum(n for _, n in patterns) != analysis["wallets"]:
        raise ValueError("Failure patterns do not cover every wallet")
    return patterns


def prior_invariance_bound(analysis: dict[str, Any]) -> dict[str, Any]:
    """Most wallets any population prior could qualify, with other gates as stored."""
    patterns = _patterns(analysis)

    def ceiling(suspended: frozenset[int]) -> int:
        return sum(n for gates, n in patterns if gates <= PRIOR_DEPENDENT_GATES | suspended)

    adequate = [(gates, n) for gates, n in patterns if not gates & (DATA_GATES | {7})]
    blockers = Counter[tuple[int, ...]]()
    for gates, n in adequate:
        blockers[tuple(sorted(gates - PRIOR_DEPENDENT_GATES))] += n
    # Stage 0 counted gates 7-13 separately; 8, 9 and 12 share one prior and move together.
    breadth = Counter[int]()
    for gates, n in patterns:
        breadth[len(gates & MODEL_ECONOMIC_GATES - PRIOR_DEPENDENT_GATES)] += n
    return {
        "prior_dependent_gates": sorted(PRIOR_DEPENDENT_GATES),
        "max_qualifiers_any_prior": {
            "production_gates": ceiling(frozenset()),
            "data_gates_suspended": ceiling(DATA_GATES),
            "clv_gate_suspended": ceiling(frozenset({13})),
            "data_and_clv_gates_suspended": ceiling(DATA_GATES | {13}),
        },
        "trusted_ess_20_wallets": sum(n for _, n in adequate),
        "trusted_ess_20_failing_clv_gate": sum(n for gates, n in adequate if 13 in gates),
        "trusted_ess_20_prior_independent_failures": [
            {"gates": list(gates), "wallets": n}
            for gates, n in sorted(blockers.items(), key=lambda x: (-x[1], x[0]))
        ],
        "prior_independent_model_economic_failure_histogram":
            {str(k): breadth[k] for k in sorted(breadth)},
    }


def _load_attrition(path: Path) -> tuple[dict[str, Any], str]:
    raw = gate_attrition._read(path)
    receipt = json.loads(raw)
    if (not isinstance(receipt, dict) or receipt.get("status") != "passed"
            or receipt.get("score_version") != "forecast-v4"
            or not isinstance(receipt.get("analysis"), dict)):
        raise ValueError("A passed forecast-v4 Stage 0 attrition receipt is required")
    return receipt, gate_attrition._digest(raw)


# ── Refit on the frozen snapshot ─────────────────────────────────────────────

def _number(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"Missing or nonfinite bet field: {field}")
    return float(value)


def load_bets(path: Path, wallets: set[str]) -> tuple[dict[str, list[Bet]], dict[str, Any]]:
    """Stream the bets export into production's event-weighted resolved bets per wallet."""
    digest, size = hashlib.sha256(), 0
    grouped: dict[str, list[Bet]] = defaultdict(list)
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            size += len(line)
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Bet record must be an object")
            wallet, status, slug = row.get("proxy_wallet"), row.get("status"), row.get("event_slug")
            if not isinstance(wallet, str) or not _WALLET.fullmatch(wallet):
                raise ValueError("Invalid bet wallet key")
            if status not in ("won", "lost", "exited", "open") or not isinstance(slug, str):
                raise ValueError("Invalid bet status or event slug")
            if status not in ("won", "lost"):
                continue
            if wallet.lower() not in wallets:
                raise ValueError("Resolved bet for a wallet missing from the score snapshot")
            price, net_size = _number(row, "entry_price"), _number(row, "net_size")
            timestamp = row.get("last_trade_ts")
            # Net size is serialized to 4 dp, so a dust position can legitimately read 0.
            if not 0 <= price <= 1 or net_size < 0 or type(timestamp) is not int or timestamp < 0:
                raise ValueError("Invalid resolved bet price, size, or timestamp")
            grouped[wallet.lower()].append(Bet(
                entry_price=price, won=status == "won", event_slug=slug,
                cost_usdc=net_size * price, ts=timestamp,
            ))
    bets = {wallet: _apply_event_weights(items) for wallet, items in grouped.items()}
    return bets, {"name": path.name, "bytes": size, "sha256": digest.hexdigest()}


def _fingerprint(path: Path) -> dict[str, Any]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return {"name": path.name, "bytes": size, "sha256": digest.hexdigest()}


def prior_gate_reasons(fit: PosteriorFit, recent_fit: PosteriorFit, *,
                       recent_sample_ok: bool) -> list[str]:
    """Production's gate 8, 9 and 12 decisions for one refit wallet."""
    reasons = []
    if fit.posterior_skill < MIN_POSTERIOR_SKILL:
        reasons.append(GATE_8_REASON)
    if fit.edge_lower_bound <= 0.0:
        reasons.append(GATE_9_REASON)
    if recent_sample_ok and recent_fit.edge_mean < 0.0:
        reasons.append(GATE_12_REASON)
    return reasons


@dataclass(frozen=True, slots=True)
class _Refit:
    row: dict[str, Any]
    unrounded: dict[str, float]


def _refit(row: dict[str, Any], bets: list[Bet], prior: PopulationPrior, now_ts: int) -> _Refit:
    fit = fit_wallet_posterior(bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2)
    recent_bets = apply_recency_weights(bets, now_ts=now_ts)
    recent = fit_wallet_posterior(recent_bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2)
    kept = [r for r in row["tailability_reasons"] if r not in PRIOR_REASONS]
    added = prior_gate_reasons(fit, recent, recent_sample_ok=GATE_11_REASON not in kept)
    reasons = sorted(kept + added, key=REASON_ORDER.__getitem__)
    unrounded = {
        "forecast_skill_likelihood": fit.posterior_skill,
        "forecast_edge_mean": fit.edge_mean,
        "forecast_edge_lower_bound": fit.edge_lower_bound,
        "recent_edge_mean": recent.edge_mean,
        "independent_settled_events": effective_sample_size(bets),
        "recent_independent_events": effective_sample_size(recent_bets),
    }
    refit = {
        **row, **{field: round(unrounded[field], 6) for field in REFIT_FIELDS},
        "skill_likelihood": round(fit.posterior_skill, 6), "edge_mean": round(fit.edge_mean, 6),
        "edge_lower_bound": round(fit.edge_lower_bound, 6),
        "recent_skill_likelihood": round(recent.posterior_skill, 6),
        "recent_edge_lower_bound": round(recent.edge_lower_bound, 6),
        "tailability_reasons": reasons,
        "tailability_status": "blocked" if reasons else "tailable",
    }
    return _Refit(refit, unrounded)


def _reproduction(rows: list[dict[str, Any]], refits: list[_Refit],
                  baseline: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Compare a production-estimator refit with the stored scores it should reproduce."""
    fields: dict[str, dict[str, float | int]] = {}
    for field in (*REFIT_FIELDS, *EVIDENCE_FIELDS):
        decimals = 4 if field in EVIDENCE_FIELDS else 6
        tolerance = 0.5 * 10 ** -decimals + 1e-12
        diffs = [abs(r.unrounded[field] - float(row[field]))
                 for row, r in zip(rows, refits, strict=True)]
        fields[field] = {"max_abs_diff": max(diffs),
                         "outside_rounding_interval": sum(d > tolerance for d in diffs)}
    flips = Counter[int]()
    for row, refit in zip(rows, refits, strict=True):
        for reason in PRIOR_REASONS & (set(row["tailability_reasons"])
                                       ^ set(refit.row["tailability_reasons"])):
            flips[next(g.number for g in gate_attrition.GATES if reason in g.reasons)] += 1
    return {
        "fields": fields,
        "gate_decision_disagreements": {str(g): flips[g] for g in sorted(PRIOR_DEPENDENT_GATES)},
        "waterfall_matches_stored": analysis["waterfall"] == baseline["waterfall"],
        "failure_patterns_match_stored":
            analysis["failure_patterns"] == baseline["failure_patterns"],
    }


def _variant_summary(key: str, estimate: PriorEstimate, analysis: dict[str, Any],
                     rows: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [{next(g.number for g in gate_attrition.GATES if r in g.reasons)
               for r in row["tailability_reasons"]} for row in rows]
    adequate = [f for f in failed if not f & (DATA_GATES | {7})]
    isolated = {str(g["gate"]): g["isolated_failures"] for g in analysis["waterfall"]}
    return {
        "variant": key, "label": ESTIMATORS[key][0], "mu": estimate.mu,
        "sigma2_raw": estimate.sigma2_raw, "sigma2": estimate.sigma2,
        "at_floor": estimate.sigma2 == _MIN_SIGMA2_POP, "fits_used": estimate.fits_used,
        "tailable_wallets": analysis["tailable_wallets"],
        "counterfactuals": analysis["counterfactuals"],
        "isolated_failures": {str(g): isolated[str(g)] for g in sorted(PRIOR_DEPENDENT_GATES)},
        "passing_all_prior_dependent_gates": sum(not f & PRIOR_DEPENDENT_GATES for f in failed),
        "trusted_ess_20_passing_prior_dependent_gates":
            sum(not f & PRIOR_DEPENDENT_GATES for f in adequate),
        "waterfall": analysis["waterfall"], "failure_patterns": analysis["failure_patterns"],
    }


def refit_variants(rows: list[dict[str, Any]], bets_by_wallet: dict[str, list[Bet]],
                   baseline: dict[str, Any]) -> dict[str, Any]:
    """Refit every wallet under each estimator and re-run the Stage 0 waterfall."""
    ordered = [bets_by_wallet.get(row["proxy_wallet"].lower(), []) for row in rows]
    fits = [f for f in map(wallet_fit, ordered) if f is not None]
    now_ts = max((bet.ts for bets in ordered for bet in bets), default=0)
    variants, reproduction = [], None
    for key, (_label, estimator) in ESTIMATORS.items():
        estimate = estimator(fits)
        prior = PopulationPrior(mu=estimate.mu, sigma2=estimate.sigma2)
        refits = [_refit(row, bets, prior, now_ts) for row, bets in zip(rows, ordered, strict=True)]
        refit_rows = [r.row for r in refits]
        analysis = gate_attrition.analyze(refit_rows)
        if key == "production":
            reproduction = _reproduction(rows, refits, baseline, analysis)
        variants.append(_variant_summary(key, estimate, analysis, refit_rows))
    no_evidence = {float(row["forecast_edge_mean"])
                   for row in rows if row.get("resolved_trades") == 0}
    return {
        "weak_prior_fits": len(fits), "recency_reference_ts": now_ts,
        "stored_prior_mean_from_no_evidence_wallets":
            no_evidence.pop() if len(no_evidence) == 1 else None,
        "reproduction": reproduction, "moment_decomposition": moment_decomposition(fits),
        "variants": variants,
    }


def _verify_output(receipt: dict[str, Any], name: str, fingerprint: dict[str, Any]) -> None:
    engines = {item.get("operation"): item for item in receipt.get("measurements", [])
               if isinstance(item, dict)}
    for engine in ("jsonl", "parquet"):
        expected = engines.get(engine, {}).get("result", {}).get("outputs", {}).get(name, {})
        if (expected.get("sha256"), expected.get("bytes")) != (
            fingerprint["sha256"], fingerprint["bytes"]
        ):
            raise ValueError(f"{name} hash/size does not match {engine} shadow evidence")


# ── Report ───────────────────────────────────────────────────────────────────

def build_report(attrition: Path, *, snapshot: Path | None = None, bets: Path | None = None,
                 benchmark: Path | None = None) -> dict[str, Any]:
    local = (snapshot, bets, benchmark)
    if any(p is not None for p in local) and not all(p is not None for p in local):
        raise ValueError("The refit needs --snapshot, --bets, and --benchmark together")
    receipt, receipt_digest = _load_attrition(attrition)
    analysis = receipt["analysis"]
    provenance: dict[str, Any] = {
        "attrition_receipt": {"name": attrition.name, "sha256": receipt_digest},
        "stage0_snapshot": receipt.get("provenance", {}).get("snapshot"),
        "diagnostic_source_sha256": gate_attrition._digest(Path(__file__).read_bytes()),
    }
    refit = None
    if snapshot is not None and bets is not None and benchmark is not None:
        stage0 = gate_attrition.build_report(snapshot, benchmark)
        expected = provenance["stage0_snapshot"] or {}
        if (stage0["provenance"]["snapshot"]["sha256"], stage0["provenance"]["snapshot"]["bytes"]) != (
            expected.get("sha256"), expected.get("bytes")
        ):
            raise ValueError("Score snapshot is not the one the attrition receipt analyzed")
        if stage0["analysis"]["waterfall"] != analysis["waterfall"]:
            raise ValueError("Recomputed Stage 0 waterfall disagrees with the attrition receipt")
        raw = gate_attrition._read(snapshot)
        if gate_attrition._digest(raw) != stage0["provenance"]["snapshot"]["sha256"]:
            raise ValueError("Score snapshot changed during analysis")
        rows = [json.loads(line) for line in raw.splitlines()]
        shadow = json.loads(gate_attrition._read(benchmark))
        bets_by_wallet, bets_fingerprint = load_bets(
            bets, {row["proxy_wallet"].lower() for row in rows})
        _verify_output(shadow, "bets", bets_fingerprint)
        refit = refit_variants(rows, bets_by_wallet, stage0["analysis"])
        if (gate_attrition._digest(gate_attrition._read(snapshot)) != gate_attrition._digest(raw)
                or _fingerprint(bets) != bets_fingerprint):
            raise ValueError("Inputs changed during analysis")
        provenance |= {"snapshot": stage0["provenance"]["snapshot"], "bets": bets_fingerprint,
                       "shadow_benchmark": stage0["provenance"]["shadow_benchmark"],
                       "scoring_code": stage0["provenance"]["scoring_code"]}
    return {
        "schema_version": 1, "status": "passed", "score_version": "forecast-v4",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": ("Stage 0 frozen cohort; population-prior estimator diagnostic only. "
                  "No production code, threshold, or default changed."),
        "refit_status": "completed" if refit else "pending_local_inputs",
        "provenance": provenance, "prior_recovery": recover_prior(analysis),
        "prior_invariance_bound": prior_invariance_bound(analysis), "refit": refit,
        "limitations": LIMITATIONS,
    }


def _g(value: float) -> str:
    return f"{value:.6g}"


def render_markdown(report: dict[str, Any]) -> str:
    recovery, bound = report["prior_recovery"], report["prior_invariance_bound"]
    low, high = recovery["sigma2_interval_from_trusted_medians"]
    point = recovery["predicted_prior_point_at_floor"]
    ceiling = bound["max_qualifiers_any_prior"]
    lines = [
        "# Stage 0 follow-up: population-prior variance floor", "",
        f"Generated: {report['generated_at']}. Score version: `{report['score_version']}`.", "",
        (f"Stage 0 receipt SHA-256: `{report['provenance']['attrition_receipt']['sha256']}`; "
         f"frozen snapshot SHA-256: `{report['provenance']['stage0_snapshot']['sha256']}`."), "",
        "## Is the fitted prior at its floor?", "",
        ("A wallet with no settled evidence reports the prior point. Hypothesis: mu is the "
         f"trusted-cohort median recent edge, {recovery['proposed_mu']}, and sigma² is the "
         f"floor, {recovery['variance_floor']}."), "",
        "| Prior-point metric at the floor | Predicted |", "|---|---:|",
        *[f"| {metric} | {value} |" for metric, value in point.items()], "",
        (f"sigma² consistent with the rounded trusted medians: [{low:.8f}, {high:.8f}]; "
         f"floor inside: **{recovery['floor_inside_interval']}**. Stored quantiles equal to "
         "a prediction: "
         + (", ".join(f"{m['cohort']} {m['metric']} {m['quantile']}"
                      for m in recovery["exact_quantile_matches"]) or "none") + "."), "",
        "## Can any prior produce a qualifier?", "",
        ("Only gates 8, 9 and 12 read the population prior. A wallet can qualify under some "
         "prior only if every gate it failed is one of those three."), "",
        "| Other gates, as stored | Most qualifiers under any prior |", "|---|---:|",
        f"| Production gates | **{ceiling['production_gates']}** |",
        f"| Data gates 1–6 suspended | {ceiling['data_gates_suspended']} |",
        f"| CLV gate 13 suspended | {ceiling['clv_gate_suspended']} |",
        f"| Data gates and gate 13 suspended | {ceiling['data_and_clv_gates_suspended']} |", "",
        (f"Of the {bound['trusted_ess_20_wallets']} data-trusted wallets with ESS ≥ 20, "
         f"{bound['trusted_ess_20_failing_clv_gate']} fail gate 13. Their failures outside "
         "gates 8, 9 and 12:"), "",
        "| Prior-independent failed gates | Wallets |", "|---|---:|",
        *[f"| {', '.join(map(str, p['gates'])) or 'none'} | {p['wallets']} |"
          for p in bound["trusted_ess_20_prior_independent_failures"]], "",
        ("Failures per wallet among model/economic gates 7, 10, 11 and 13, which no prior "
         "changes (all wallets):"), "",
        "| Prior-independent model/economic failures | Wallets |", "|---:|---:|",
        *[f"| {k} | {n} |"
          for k, n in bound["prior_independent_model_economic_failure_histogram"].items()], "",
        "## Estimator refit", "",
    ]
    refit = report["refit"]
    if refit is None:
        lines += [
            ("**Pending local inputs.** The refit needs the frozen `enrichments.jsonl` and "
             "`bets.jsonl` from the verified enrichment shadow run; neither is in git. Run on "
             "the machine that holds them:"), "",
            "```powershell",
            ".\\.venv\\Scripts\\python.exe -m marketsignalos_polymarket.prior_floor `",
            "  --attrition docs/benchmarks/2026-09-10-gate-attrition.json `",
            "  --snapshot C:/path/to/enrichment-shadow/parquet/enrichments.jsonl `",
            "  --bets C:/path/to/enrichment-shadow/parquet/bets.jsonl `",
            "  --benchmark docs/benchmarks/2026-09-08-enrichment-shadow.json `",
            "  --output benchmark-output/prior-floor/prior-floor.json",
            "```", "",
        ]
    else:
        repro, decomposition = refit["reproduction"], refit["moment_decomposition"]
        lines += [
            (f"{refit['weak_prior_fits']} wallets have at least 3 resolved bets and enter every "
             f"estimator. Recency reference: {refit['recency_reference_ts']}. Stored prior mean "
             f"from no-evidence wallets: {refit['stored_prior_mean_from_no_evidence_wallets']}."),
            "",
            ("| Variant | mu | sigma² raw | sigma² applied | Fits | Qualifiers | "
             "Data gates suspended | CLV-5 qualifiers | Gate 8 fails | Gate 9 fails | "
             "Gate 12 fails | Pass 8, 9, 12 | Trusted ESS≥20 pass 8, 9, 12 |"),
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for v in refit["variants"]:
            iso, cf = v["isolated_failures"], v["counterfactuals"]
            clv = cf["clv_minimum_five"]
            lines.append(
                f"| {v['label']} | {_g(v['mu'])} | {_g(v['sigma2_raw'])} | {_g(v['sigma2'])} | "
                f"{v['fits_used']} | **{v['tailable_wallets']}** | "
                f"**{cf['data_gates_suspended']}** | "
                f"{clv['qualified_min']}–{clv['qualified_max']} | {iso['8']} | {iso['9']} | "
                f"{iso['12']} | {v['passing_all_prior_dependent_gates']} | "
                f"{v['trusted_ess_20_passing_prior_dependent_gates']} |")
        names = [v["variant"] for v in refit["variants"]]
        lines += ["", "Cumulative waterfall, wallets remaining after each gate:", "",
                  "| Gate | " + " | ".join(names) + " |",
                  "|---|" + "---:|" * len(names)]
        for index, gate in enumerate(gate_attrition.GATES):
            lines.append(f"| {gate.number} | " + " | ".join(
                str(v["waterfall"][index]["remaining"]) for v in refit["variants"]) + " |")
        lines += ["", "### Reproduction check (production estimator)", "",
                  "| Field | Max abs diff | Outside stored rounding |", "|---|---:|---:|",
                  *[f"| {field} | {_g(item['max_abs_diff'])} | "
                    f"{item['outside_rounding_interval']} |"
                    for field, item in repro["fields"].items()], "",
                  (f"Gate decision disagreements: {repro['gate_decision_disagreements']}. "
                   f"Waterfall matches stored: **{repro['waterfall_matches_stored']}**; "
                   f"failure patterns match: **{repro['failure_patterns_match_stored']}**."), "",
                  "### Production moment decomposition", "",
                  "| Quantity | Value |", "|---|---:|",
                  *[f"| {key.replace('_', ' ')} | "
                    f"{_g(value) if isinstance(value, float) else value} |"
                    for key, value in decomposition.items()], ""]
    lines += ["## Scope and verification", "", *[f"- {s}" for s in report["limitations"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attrition", type=Path, required=True,
                        help="Committed Stage 0 gate-attrition JSON receipt")
    parser.add_argument("--snapshot", type=Path, help="Frozen enrichments.jsonl (refit)")
    parser.add_argument("--bets", type=Path, help="bets.jsonl from the same shadow run (refit)")
    parser.add_argument("--benchmark", type=Path, help="Enrichment shadow receipt (refit)")
    parser.add_argument("--output", type=Path, required=True,
                        help="New .json report path; also writes .md")
    args = parser.parse_args()
    json_path = args.output
    md_path = json_path.with_suffix(".md")
    if json_path.suffix != ".json" or json_path.exists() or md_path.exists():
        parser.error("Output must be a new .json path with no existing .md companion")
    report = build_report(args.attrition, snapshot=args.snapshot, bets=args.bets,
                          benchmark=args.benchmark)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with md_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(render_markdown(report))


if __name__ == "__main__":
    main()
