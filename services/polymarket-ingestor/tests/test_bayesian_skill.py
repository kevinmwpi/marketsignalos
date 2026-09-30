"""Regression tests for the Bayesian edge model's numerical stability.

These pin the fix for the production failure where an undamped Newton step
diverged on near-flat objectives: a few separable wallets produced
billion-scale MLEs, which poisoned the empirical-Bayes population prior
(mu~67, sigma2~1e7), which in turn drove *every* wallet's fit to a garbage
edge (~1e10) and skill_likelihood == 1.0 for 100% of wallets.
"""
from __future__ import annotations

import math

from marketsignalos_polymarket.bayesian_skill import (
    _EDGE_CLAMP,
    _MAX_SIGMA2_POP,
    _MIN_SIGMA2_POP,
    Bet,
    fit_population_prior,
    fit_wallet_posterior,
)


def test_fit_stays_bounded_under_diffuse_off_center_prior() -> None:
    """The exact production trigger: an off-centre diffuse prior. Undamped
    Newton sent the MAP to ~1e10; the damped line search must converge to the
    true, bounded MAP (~0 for a 50/50 wallet whose data overwhelms the weak
    prior)."""
    bets = [Bet(entry_price=0.5, won=(i % 2 == 0)) for i in range(100)]
    fit = fit_wallet_posterior(bets, mu_prior=67.6, sigma2_prior=1.07e7)
    assert math.isfinite(fit.edge_mean)
    assert abs(fit.edge_mean) <= _EDGE_CLAMP
    assert 0.0 <= fit.posterior_skill <= 1.0
    assert abs(fit.edge_mean) < 1.0


def test_separable_wallet_edge_is_finite_and_bounded() -> None:
    """An all-win wallet's likelihood grows without bound; the fit must return
    a finite edge inside the physical domain rather than diverging."""
    bets = [Bet(entry_price=0.5, won=True) for _ in range(200)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=1.07e7)
    assert math.isfinite(fit.edge_mean)
    assert 0.0 < fit.edge_mean <= _EDGE_CLAMP
    assert 0.0 <= fit.posterior_skill <= 1.0


def test_population_prior_bounded_by_degenerate_wallet() -> None:
    """One near-separable wallet must not blow up the empirical-Bayes prior
    (previously it produced mu~67, sigma2~1e7)."""
    normal = [
        [Bet(entry_price=0.5, won=(j % 2 == 0)) for j in range(12)]
        for _ in range(30)
    ]
    separable = [Bet(entry_price=0.5, won=True) for _ in range(60)]
    prior = fit_population_prior(normal + [separable])
    assert math.isfinite(prior.mu) and abs(prior.mu) <= _EDGE_CLAMP
    assert _MIN_SIGMA2_POP <= prior.sigma2 <= _MAX_SIGMA2_POP


def test_fit_recovers_a_known_edge() -> None:
    """Sanity: a wallet that beats a 50c line 73/100 has a true edge of
    logit(0.73) ~ 0.99; the line search must still find it (no over-shrinkage
    from the stability guards)."""
    bets = [Bet(entry_price=0.5, won=(i < 73)) for i in range(100)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=100.0)
    assert 0.7 < fit.edge_mean < 1.3
    assert fit.posterior_skill > 0.9


def test_null_wallet_scores_near_half() -> None:
    """A wallet that exactly matches the market (50/50 at a 50c line) should
    look indistinguishable from it: edge ~ 0, skill ~ 0.5."""
    bets = [Bet(entry_price=0.5, won=(i % 2 == 0)) for i in range(200)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=0.25)
    assert abs(fit.edge_mean) < 0.15
    assert 0.4 < fit.posterior_skill < 0.6
