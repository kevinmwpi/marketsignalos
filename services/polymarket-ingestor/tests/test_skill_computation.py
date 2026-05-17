from __future__ import annotations

import math

from marketsignalos_polymarket.bayesian_skill import (
    Bet,
    PopulationPrior,
    effective_sample_size,
    fit_population_prior,
    fit_wallet_posterior,
    rank_score,
)
from marketsignalos_polymarket.models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
)
from marketsignalos_polymarket.skill_computation import (
    _market_winning_outcome,
    compute_all_enrichment,
    compute_wallet_enrichment,
)


def _trade(
    cond: str,
    outcome: int,
    side: str,
    size: float,
    price: float,
    ts: int = 1000,
    event_slug: str = "",
) -> PolymarketActivity:
    return PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=ts,
        condition_id=cond,
        type="TRADE",
        side=side,
        size=size,
        usdc_size=size * price,
        price=price,
        outcome_index=outcome,
        outcome="Yes" if outcome == 0 else "No",
        slug="",
        title="",
        event_slug=event_slug or f"event-{cond}",
        transaction_hash=f"0x{ts}{cond[:4]}{outcome}",
    )


def _market(cond: str, *, closed: bool, winning_outcome: int | None) -> PolymarketMarket:
    if winning_outcome is None:
        prices = [0.0, 0.0]  # canceled
    else:
        prices = [1.0 if i == winning_outcome else 0.0 for i in range(2)]
    return PolymarketMarket(
        gamma_id=cond,
        condition_id=cond,
        slug=f"m-{cond}",
        question="?",
        category="",
        end_date="2026-01-01T00:00:00Z",
        outcomes=["Yes", "No"],
        outcome_prices=prices,
        volume_usdc=1000,
        liquidity_usdc=100,
        closed=closed,
        active=not closed,
        last_trade_price=None,
        best_bid=None,
        best_ask=None,
    )


# ── bayesian_skill: pure math ────────────────────────────────────────────────

def test_fit_with_no_bets_returns_prior() -> None:
    fit = fit_wallet_posterior([], mu_prior=0.0, sigma2_prior=0.25)
    assert fit.edge_mean == 0.0
    assert fit.edge_var == 0.25
    # 0 edge → posterior_skill = 0.5
    assert abs(fit.posterior_skill - 0.5) < 1e-9


def test_fit_wins_at_market_odds_implies_positive_edge() -> None:
    # 10 bets at 50¢, all winners. MAP edge should be strongly positive, but the
    # Laplace approximation gets a WIDE posterior because the likelihood has no
    # upper curvature when every observation is a win (q→1 makes q*(1-q)→0).
    # That's correct behaviour — the model can't distinguish "moderate edge" from
    # "huge edge" from this data alone.
    bets = [Bet(entry_price=0.5, won=True) for _ in range(10)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=100.0)
    assert fit.edge_mean > 1.0
    assert fit.posterior_skill > 0.85


def test_fit_favorites_only_no_edge() -> None:
    # A wallet that only buys 90¢ favorites and wins 9/10 — they're betting
    # on the market, not against it. Edge should hover near zero, not high.
    bets = [Bet(entry_price=0.9, won=True) for _ in range(9)] + [
        Bet(entry_price=0.9, won=False)
    ]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=100.0)
    # The MLE finds the edge that makes 9/10 the expected outcome at p=0.9 —
    # which is exactly 0. Any noise should be small.
    assert abs(fit.edge_mean) < 0.2
    assert fit.posterior_skill < 0.7


def test_fit_underperforming_market_negative_edge() -> None:
    # Wallet buys 50¢ markets and wins 2/10 — clearly worse than random.
    bets = [Bet(entry_price=0.5, won=True) for _ in range(2)] + [
        Bet(entry_price=0.5, won=False) for _ in range(8)
    ]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=100.0)
    assert fit.edge_mean < -1.0
    assert fit.posterior_skill < 0.05
    assert fit.edge_lower_bound < 0


def test_bayesian_shrinkage_makes_5_5_unimpressive() -> None:
    """5 wins at 50¢, with a tight population prior, should NOT look elite."""
    bets = [Bet(entry_price=0.5, won=True) for _ in range(5)]
    # Tight prior — population edges are believed to be near zero.
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=0.04)
    assert fit.edge_mean > 0  # data still pushes it positive
    # ...but the posterior is dragged back substantially by the prior.
    assert fit.edge_mean < 0.5
    # Conservative lower bound stays modest.
    assert fit.edge_lower_bound < 0.3


def test_lots_of_data_overwhelms_prior() -> None:
    """A wallet with 200 resolved bets and a moderate prior should sit close to MLE."""
    bets = [Bet(entry_price=0.5, won=True) for _ in range(150)] + [
        Bet(entry_price=0.5, won=False) for _ in range(50)
    ]
    # MLE for 75% win rate = logit(0.75) = 1.099. With moderate prior the
    # MAP should be close to MLE.
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=1.0)
    assert fit.edge_mean > 1.0
    assert fit.posterior_skill > 0.999
    assert fit.edge_lower_bound > 0


def test_edge_lower_bound_below_mean() -> None:
    bets = [Bet(entry_price=0.5, won=True) for _ in range(10)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=1.0)
    assert fit.edge_lower_bound < fit.edge_mean


def test_fit_handles_zero_price_safely() -> None:
    """Entry price clamped to (eps, 1-eps) so logit is finite."""
    bets = [Bet(entry_price=0.0, won=True), Bet(entry_price=1.0, won=False)]
    fit = fit_wallet_posterior(bets, mu_prior=0.0, sigma2_prior=1.0)
    assert math.isfinite(fit.edge_mean)
    assert math.isfinite(fit.edge_lower_bound)


# ── empirical Bayes ──────────────────────────────────────────────────────────

def test_population_prior_falls_back_when_too_few_wallets() -> None:
    prior = fit_population_prior([])
    assert prior == PopulationPrior(mu=0.0, sigma2=0.25)


def test_population_prior_recovers_spread() -> None:
    """Mix of skilled and unskilled wallets — EB should report a non-trivial sigma2."""
    skilled = [Bet(entry_price=0.5, won=True) for _ in range(30)]
    unskilled = [Bet(entry_price=0.5, won=False) for _ in range(30)]
    average = [Bet(entry_price=0.5, won=(i % 2 == 0)) for i in range(30)]
    prior = fit_population_prior([skilled, unskilled, average])
    # Population spread should be much larger than the floor.
    assert prior.sigma2 > 0.5


# ── effective_sample_size ────────────────────────────────────────────────────

def test_ess_empty() -> None:
    assert effective_sample_size([]) == 0.0


def test_ess_all_unique_events_is_n() -> None:
    bets = [Bet(entry_price=0.5, won=True, event_slug=f"e{i}") for i in range(5)]
    assert effective_sample_size(bets) == 5.0


def test_ess_repeated_event_discounts() -> None:
    bets = [Bet(entry_price=0.5, won=True, event_slug="same-event") for _ in range(5)]
    # 1 unique event + 4 repeats * 0.5 = 3.
    assert effective_sample_size(bets) == 3.0


def test_ess_falls_back_when_event_slugs_missing() -> None:
    bets = [Bet(entry_price=0.5, won=True, event_slug="") for _ in range(5)]
    assert effective_sample_size(bets) == 5.0


# ── rank_score ───────────────────────────────────────────────────────────────

def test_rank_score_zero_when_lower_bound_negative() -> None:
    assert rank_score(posterior_skill=0.9, edge_lower_bound=-0.1, resolved_volume_usdc=1000) == 0.0


def test_rank_score_grows_with_volume() -> None:
    low = rank_score(posterior_skill=0.95, edge_lower_bound=0.2, resolved_volume_usdc=100)
    high = rank_score(posterior_skill=0.95, edge_lower_bound=0.2, resolved_volume_usdc=100_000)
    assert high > low


def test_rank_score_grows_with_posterior_skill() -> None:
    weak = rank_score(posterior_skill=0.55, edge_lower_bound=0.2, resolved_volume_usdc=10_000)
    strong = rank_score(posterior_skill=0.99, edge_lower_bound=0.2, resolved_volume_usdc=10_000)
    assert strong > weak


# ── _market_winning_outcome ──────────────────────────────────────────────────

def test_open_market_has_no_winner() -> None:
    open_market = PolymarketMarket(
        gamma_id="x", condition_id="0xc", slug="", question="",
        category="", end_date="", outcomes=["Yes", "No"],
        outcome_prices=[0.6, 0.4],
        volume_usdc=0, liquidity_usdc=0, closed=False, active=True,
        last_trade_price=None, best_bid=None, best_ask=None,
    )
    assert _market_winning_outcome(open_market) is None


def test_canceled_market_returns_none() -> None:
    m = _market("0xc", closed=True, winning_outcome=None)
    assert _market_winning_outcome(m) is None


def test_resolved_yes_market_returns_zero() -> None:
    m = _market("0xc", closed=True, winning_outcome=0)
    assert _market_winning_outcome(m) == 0


def test_resolved_no_market_returns_one() -> None:
    m = _market("0xc", closed=True, winning_outcome=1)
    assert _market_winning_outcome(m) == 1


# ── compute_wallet_enrichment ────────────────────────────────────────────────

def test_winning_yes_bet_counted_as_win() -> None:
    activity = [_trade("0xc1", outcome=0, side="BUY", size=100, price=0.4)]
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=0)}
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.wins == 1
    assert e.losses == 0
    assert e.resolved_trades == 1
    assert e.win_rate == 1.0
    # PnL: 100 shares paid $1 each (=$100), cost was $40 → +$60
    assert e.total_pnl_usdc == 60.0
    # Bayesian: edge is positive after winning a 40¢ bet; finite output.
    assert e.edge_mean > 0
    assert math.isfinite(e.edge_lower_bound)


def test_losing_yes_bet_counted_as_loss() -> None:
    activity = [_trade("0xc1", outcome=0, side="BUY", size=100, price=0.4)]
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=1)}  # NO won
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.wins == 0
    assert e.losses == 1
    # Lost the full $40 entry cost.
    assert e.total_pnl_usdc == -40.0


def test_open_position_not_counted_in_resolved_trades() -> None:
    activity = [_trade("0xc1", outcome=0, side="BUY", size=100, price=0.4)]
    markets = {"0xc1": _market("0xc1", closed=False, winning_outcome=None)}
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.resolved_trades == 0
    assert e.wins == 0
    assert e.losses == 0
    # Total volume still includes the unresolved trade.
    assert e.total_volume_usdc == 40.0
    # No bets to fit on — edge stays at the prior mean.
    assert e.edge_mean == 0.0


def test_full_exit_before_resolution_excluded_but_pnl_realized() -> None:
    activity = [
        _trade("0xc1", outcome=0, side="BUY", size=100, price=0.4, ts=1000),
        _trade("0xc1", outcome=0, side="SELL", size=100, price=0.6, ts=2000),
    ]
    # Even though market resolved YES, the wallet was flat at resolution.
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=0)}
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.resolved_trades == 0
    # Realized PnL = 60 (sell proceeds) - 40 (cost) = +20.
    assert e.total_pnl_usdc == 20.0
    # No resolved volume because no bet survived to resolution.
    assert e.resolved_volume_usdc == 0.0


def test_partial_exit_then_resolution_counts_remaining() -> None:
    activity = [
        _trade("0xc1", outcome=0, side="BUY", size=100, price=0.4, ts=1000),
        _trade("0xc1", outcome=0, side="SELL", size=50, price=0.5, ts=2000),
    ]
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=0)}
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    # 50 shares held → resolved YES → 1 resolved bet (win)
    assert e.resolved_trades == 1
    assert e.wins == 1
    # Realized: sold 50 @ 0.5 = 25, cost basis 50 * 0.4 = 20 → +5 realized
    # Unrealized: 50 shares pay $50, remaining cost 50 * 0.4 = 20 → +30
    # Total: +35
    assert e.total_pnl_usdc == 35.0
    # resolved_volume_usdc = remaining cost basis = 20.
    assert e.resolved_volume_usdc == 20.0


def test_three_wins_one_loss_skill_likelihood_above_half() -> None:
    activity = [
        _trade("0xa", outcome=0, side="BUY", size=10, price=0.5, ts=1),
        _trade("0xb", outcome=0, side="BUY", size=10, price=0.5, ts=2),
        _trade("0xc", outcome=0, side="BUY", size=10, price=0.5, ts=3),
        _trade("0xd", outcome=0, side="BUY", size=10, price=0.5, ts=4),
    ]
    markets = {
        "0xa": _market("0xa", closed=True, winning_outcome=0),
        "0xb": _market("0xb", closed=True, winning_outcome=0),
        "0xc": _market("0xc", closed=True, winning_outcome=0),
        "0xd": _market("0xd", closed=True, winning_outcome=1),
    }
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.wins == 3 and e.losses == 1
    assert e.win_rate == 0.75
    assert e.skill_likelihood > 0.5
    assert e.edge_mean > 0


def test_canceled_market_excluded_from_win_loss() -> None:
    activity = [_trade("0xc1", outcome=0, side="BUY", size=10, price=0.5)]
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=None)}
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.resolved_trades == 0


def test_redeem_events_dont_corrupt_win_count() -> None:
    """REDEEM rows have outcomeIndex=999 — must not be treated as trades."""
    trade = _trade("0xc1", outcome=0, side="BUY", size=100, price=0.4)
    redeem = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=2000,
        condition_id="0xc1",
        type="REDEEM",
        side="",
        size=100,
        usdc_size=100,
        price=1,
        outcome_index=999,
        outcome="",
        slug="",
        title="",
        event_slug="",
        transaction_hash="0xredeem",
    )
    markets = {"0xc1": _market("0xc1", closed=True, winning_outcome=0)}
    e = compute_wallet_enrichment("0xabc", activity=[trade, redeem], markets_by_condition=markets)
    # Still exactly 1 resolved bet — the REDEEM doesn't create a phantom second one.
    assert e.resolved_trades == 1
    assert e.wins == 1
    # And trade_count counts only actual TRADE events.
    assert e.trade_count == 1


def test_favorites_only_wallet_has_near_zero_edge() -> None:
    """
    A wallet that buys 90¢ favorites and wins 9/10 is taking exactly the
    market's own bets. After the Bayesian fit the edge should be near zero
    — NOT high — even though the raw win_rate is 90%.
    """
    # 10 different markets, all 90¢ favorites, wallet wins 9.
    activity: list[PolymarketActivity] = []
    markets: dict[str, PolymarketMarket] = {}
    for i in range(10):
        cond = f"0xf{i}"
        activity.append(_trade(cond, outcome=0, side="BUY", size=10, price=0.9, ts=i))
        # First 9 markets resolved YES (favorite wins), last one resolved NO.
        winner = 0 if i < 9 else 1
        markets[cond] = _market(cond, closed=True, winning_outcome=winner)
    e = compute_wallet_enrichment("0xabc", activity=activity, markets_by_condition=markets)
    assert e.wins == 9 and e.losses == 1
    assert e.win_rate == 0.9
    # The raw 90% win rate would have made the old skill_likelihood ~ 1.0.
    # The Bayesian model says: 9/10 at 90¢ markets is exactly expected; edge ≈ 0.
    assert abs(e.edge_mean) < 0.3, f"edge_mean was {e.edge_mean}, expected near zero"


# ── compute_all_enrichment ────────────────────────────────────────────────────

def test_compute_all_buckets_by_wallet_and_attaches_name() -> None:
    activity = [
        PolymarketActivity(
            proxy_wallet="0xtheo",
            timestamp=1, condition_id="0xc1", type="TRADE",
            side="BUY", size=10, usdc_size=4, price=0.4,
            outcome_index=0, outcome="Yes", slug="", title="", event_slug="e1",
            transaction_hash="0xa",
        ),
        PolymarketActivity(
            proxy_wallet="0xfredi",
            timestamp=1, condition_id="0xc1", type="TRADE",
            side="BUY", size=10, usdc_size=6, price=0.6,
            outcome_index=1, outcome="No", slug="", title="", event_slug="e1",
            transaction_hash="0xb",
        ),
    ]
    markets = [_market("0xc1", closed=True, winning_outcome=0)]
    leaderboard = [
        PolymarketLeaderboardEntry(
            proxy_wallet="0xtheo", name="Theo4", pseudonym="Theo4",
            amount_usdc=1, metric="profit", window="all",
        ),
    ]

    out = compute_all_enrichment(activity=activity, markets=markets, leaderboard=leaderboard)
    by_wallet = {e.proxy_wallet: e for e in out}
    assert by_wallet["0xtheo"].name == "Theo4"
    assert by_wallet["0xtheo"].wins == 1
    assert by_wallet["0xfredi"].name == ""  # not on leaderboard sample
    assert by_wallet["0xfredi"].losses == 1


def test_compute_all_sorts_by_rank_score_descending() -> None:
    """
    Wallet A: bought at 50¢ and won 6/6. Wallet B: bought at 50¢ and lost 6/6.
    A should rank above B by rank_score (which is zero for B).
    """
    def trades_for(wallet: str, outcome: int) -> list[PolymarketActivity]:
        return [
            PolymarketActivity(
                proxy_wallet=wallet, timestamp=i, condition_id=f"0xc{i}",
                type="TRADE", side="BUY", size=10, usdc_size=5, price=0.5,
                outcome_index=outcome, outcome="", slug="", title="",
                event_slug=f"event-{i}",
                transaction_hash=f"0x{wallet}{i}",
            ) for i in range(6)
        ]

    # A picks YES (which wins); B picks NO (which loses).
    activity = trades_for("0xa", outcome=0) + trades_for("0xb", outcome=1)
    markets = [_market(f"0xc{i}", closed=True, winning_outcome=0) for i in range(6)]
    out = compute_all_enrichment(activity=activity, markets=markets, leaderboard=[])
    assert out[0].proxy_wallet == "0xa"
    assert out[-1].proxy_wallet == "0xb"
    assert out[0].rank_score > out[-1].rank_score


def test_compute_all_populates_new_fields() -> None:
    """Every enrichment row carries the full Bayesian field set.

    We use a 16/4 wallet (not 8/0) so the likelihood has curvature in both
    directions; that keeps the Laplace posterior narrow enough to give a
    strictly-positive lower bound.
    """
    win_acts = [
        PolymarketActivity(
            proxy_wallet="0xwin", timestamp=i, condition_id=f"0xc{i}",
            type="TRADE", side="BUY", size=100, usdc_size=50.0, price=0.5,
            outcome_index=0, outcome="Yes", slug="", title="",
            event_slug=f"event-{i}",
            transaction_hash=f"0xw{i}",
        )
        for i in range(16)
    ]
    loss_acts = [
        PolymarketActivity(
            proxy_wallet="0xwin", timestamp=20 + i, condition_id=f"0xL{i}",
            type="TRADE", side="BUY", size=100, usdc_size=50.0, price=0.5,
            outcome_index=0, outcome="Yes", slug="", title="",
            event_slug=f"event-loss-{i}",
            transaction_hash=f"0xL{i}",
        )
        for i in range(4)
    ]
    win_markets = [_market(f"0xc{i}", closed=True, winning_outcome=0) for i in range(16)]
    loss_markets = [_market(f"0xL{i}", closed=True, winning_outcome=1) for i in range(4)]
    out = compute_all_enrichment(
        activity=win_acts + loss_acts,
        markets=win_markets + loss_markets,
        leaderboard=[],
    )
    row = out[0]
    assert row.proxy_wallet == "0xwin"
    assert row.resolved_trades == 20
    assert row.wins == 16 and row.losses == 4
    assert row.resolved_volume_usdc == 20 * 50.0
    assert row.effective_sample_size == 20.0  # 20 unique events
    assert row.edge_mean > 0
    assert row.edge_lower_bound > 0
    assert row.skill_likelihood > 0.95
    assert row.rank_score > 0
