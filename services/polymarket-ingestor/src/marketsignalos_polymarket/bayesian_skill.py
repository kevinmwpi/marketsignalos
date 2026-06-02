"""
Bayesian edge model for per-wallet skill.

Model
-----
For each resolved bet i placed by wallet w:

    p_i = market-implied probability at entry (avg cost / share)
    y_i = 1 if the bet won, 0 if it lost

    y_i ~ Bernoulli(q_i)
    logit(q_i) = logit(p_i) + edge_w

So `edge_w` is the wallet's log-odds adjustment relative to the market's
own implied odds:
  - edge_w > 0  → beats the market
  - edge_w = 0  → indistinguishable from the market
  - edge_w < 0  → loses to the market

Hierarchy / shrinkage
---------------------
We assume edges are drawn from a population:

    edge_w ~ Normal(mu_pop, sigma2_pop)

mu_pop and sigma2_pop are fit by Empirical Bayes (method-of-moments from
per-wallet MLEs). A wallet with little data gets shrunk toward mu_pop;
a wallet with many resolved bets carries enough Fisher information that
the prior barely matters.

Inference: Laplace approximation
---------------------------------
The posterior of edge_w is approximately Normal, parameterised by the
MAP estimate (Newton's method on a concave objective) and the inverse
of the negative Hessian at the MAP. From that we derive:

    posterior_skill   = P(edge_w > 0 | data)
    edge_mean         = posterior MAP estimate
    edge_lower_bound  = 5th percentile of the posterior

Why Laplace and not MCMC: the per-wallet log-posterior is the sum of a
log-concave logistic likelihood and a Gaussian prior, so it's
unimodal and the Normal approximation is excellent in practice. No
scipy dependency required; everything below uses stdlib `math`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Standard-normal CDF / inverse used to translate posterior-stddev units
# into probabilities and quantiles.
_NORM_05 = 1.6448536269514722  # Phi^{-1}(0.95) — 5th-percentile lower bound

# Clamp prices into (eps, 1-eps) so logit doesn't blow up on 0 / 1.
_PRICE_EPS = 1e-3

# Floor for sigma2_pop so the prior never collapses to a point mass.
_MIN_SIGMA2_POP = 0.01

# Initial weakly-informative prior used to compute per-wallet MLEs. Wide
# enough that the prior barely shrinks the estimate for wallets with
# meaningful sample size; narrow enough that all-win / all-loss wallets
# return a finite MLE.
_WEAK_PRIOR_SIGMA2 = 100.0


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _safe_logit(p: float) -> float:
    """logit(p) with p clamped to (eps, 1-eps) so the result is finite."""
    p_clamped = min(max(p, _PRICE_EPS), 1.0 - _PRICE_EPS)
    return math.log(p_clamped / (1.0 - p_clamped))


def _sigmoid(x: float) -> float:
    # Numerically stable sigmoid.
    if x >= 0:
        ex = math.exp(-x)
        return 1.0 / (1.0 + ex)
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ── Per-wallet posterior fit ─────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class Bet:
    """One resolved bet contributing to a wallet's edge estimate."""

    entry_price: float       # market-implied probability of the picked outcome
    won: bool                # True if that outcome resolved YES
    event_slug: str = ""     # used only by effective_sample_size()
    cost_usdc: float = 0.0   # used for rank_score weighting
    weight: float = 1.0      # event-capped likelihood contribution


@dataclass(slots=True, frozen=True)
class PosteriorFit:
    """Result of fitting one wallet's edge posterior."""

    edge_mean: float          # MAP estimate of edge_w in log-odds
    edge_var: float           # Laplace posterior variance
    posterior_skill: float    # P(edge_w > 0 | data)
    edge_lower_bound: float   # 5th-percentile lower bound of edge_w
    edge_z: float             # edge_mean / sqrt(edge_var) — diagnostic z


def fit_wallet_posterior(
    bets: list[Bet],
    *,
    mu_prior: float,
    sigma2_prior: float,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> PosteriorFit:
    """
    Fit the posterior over `edge` for one wallet's resolved bets, using
    Newton's method on the MAP objective:

        L(edge) = sum_i [y_i log q_i + (1-y_i) log(1-q_i)]
                  - (edge - mu_prior)^2 / (2 * sigma2_prior)

    The objective is concave (sum of log-sigmoid + a Gaussian penalty),
    so Newton converges to the unique MAP from any starting point.

    A wallet with zero resolved bets gets back the prior unchanged.
    """
    if not bets:
        sigma = math.sqrt(sigma2_prior)
        return PosteriorFit(
            edge_mean=mu_prior,
            edge_var=sigma2_prior,
            posterior_skill=_normal_cdf(mu_prior / sigma) if sigma > 0 else 0.5,
            edge_lower_bound=mu_prior - _NORM_05 * sigma,
            edge_z=mu_prior / sigma if sigma > 0 else 0.0,
        )

    logits = [_safe_logit(b.entry_price) for b in bets]
    ys = [1.0 if b.won else 0.0 for b in bets]
    weights = [max(0.0, b.weight) for b in bets]
    inv_sigma2 = 1.0 / sigma2_prior

    edge = mu_prior
    for _ in range(max_iter):
        # q_i = sigmoid(logit_i + edge); accumulate gradient and Hessian.
        grad_data = 0.0
        hess_data = 0.0
        for li, yi, weight in zip(logits, ys, weights):
            qi = _sigmoid(li + edge)
            grad_data += weight * (yi - qi)
            hess_data -= weight * qi * (1.0 - qi)
        gradient = grad_data - (edge - mu_prior) * inv_sigma2
        hessian = hess_data - inv_sigma2  # strictly negative
        step = -gradient / hessian
        edge_new = edge + step
        if abs(step) < tol:
            edge = edge_new
            break
        edge = edge_new

    # Final Hessian evaluation for the Laplace variance.
    info = inv_sigma2
    for li, weight in zip(logits, weights):
        qi = _sigmoid(li + edge)
        info += weight * qi * (1.0 - qi)
    edge_var = 1.0 / info
    edge_sd = math.sqrt(edge_var)
    return PosteriorFit(
        edge_mean=edge,
        edge_var=edge_var,
        posterior_skill=_normal_cdf(edge / edge_sd) if edge_sd > 0 else 0.5,
        edge_lower_bound=edge - _NORM_05 * edge_sd,
        edge_z=edge / edge_sd if edge_sd > 0 else 0.0,
    )


# ── Empirical-Bayes population prior ─────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class PopulationPrior:
    mu: float
    sigma2: float


def fit_population_prior(per_wallet_bets: list[list[Bet]]) -> PopulationPrior:
    """
    Fit (mu_pop, sigma2_pop) by method of moments from per-wallet MLEs.

    Under the assumption that wallet edges are drawn from N(mu, sigma2)
    and the MLE estimate for wallet w has sampling variance v_w:

        E[MLE_w]   = mu
        Var[MLE_w] = sigma2 + E[v_w]

    so:
        mu_hat     = mean(MLE_w)
        sigma2_hat = max(eps, var(MLE_w) - mean(v_w))

    Wallets with fewer than 3 resolved bets are excluded from the
    population fit because their MLE is dominated by the weak prior we
    use as a floor.

    If fewer than two wallets clear the bar, we fall back to a
    weakly-informative prior centred at zero.
    """
    fits: list[PosteriorFit] = []
    for bets in per_wallet_bets:
        if len(bets) < 3:
            continue
        fits.append(
            fit_wallet_posterior(
                bets,
                mu_prior=0.0,
                sigma2_prior=_WEAK_PRIOR_SIGMA2,
            )
        )

    if len(fits) < 2:
        return PopulationPrior(mu=0.0, sigma2=0.25)

    mu = sum(f.edge_mean for f in fits) / len(fits)
    total_var = sum((f.edge_mean - mu) ** 2 for f in fits) / len(fits)
    within_var = sum(f.edge_var for f in fits) / len(fits)
    sigma2 = max(_MIN_SIGMA2_POP, total_var - within_var)
    return PopulationPrior(mu=mu, sigma2=sigma2)


# ── Effective sample size & rank score ───────────────────────────────────────

def effective_sample_size(bets: list[Bet]) -> float:
    """
    Sum event-capped likelihood weights.

    Skill computation gives each independent event one total vote and
    distributes it across legs by at-risk capital. Blank event slugs are
    treated as independent observations and retain their default weight.
    """
    return sum(max(0.0, b.weight) for b in bets)


def rank_score(
    posterior_skill: float,
    edge_lower_bound: float,
    resolved_volume_usdc: float,
) -> float:
    """
    Conservative ranking score that avoids inflating a wallet that
    merely piles into 98-cent favourites:

        rank = posterior_skill * max(0, edge_lower_bound)
                                  * log1p(resolved_volume_usdc)

    Components:
      - posterior_skill makes a 5/5 wallet look unimpressive after
        shrinkage (small ESS / wide posterior).
      - max(0, edge_lower_bound) hard-zeros wallets we cannot
        conservatively say beat the market.
      - log1p(volume) rewards wallets putting real money behind it.
    """
    edge_floor = max(0.0, edge_lower_bound)
    if edge_floor <= 0.0 or posterior_skill <= 0.0:
        return 0.0
    return posterior_skill * edge_floor * math.log1p(max(0.0, resolved_volume_usdc))
