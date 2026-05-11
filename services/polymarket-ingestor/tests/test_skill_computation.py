from __future__ import annotations

from marketsignalos_polymarket.models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
)
from marketsignalos_polymarket.skill_computation import (
    _binomial_skill,
    _market_winning_outcome,
    compute_all_enrichment,
    compute_wallet_enrichment,
)


def _trade(
    cond: str, outcome: int, side: str, size: float, price: float, ts: int = 1000
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
        event_slug="",
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


# ── _binomial_skill ──────────────────────────────────────────────────────────

def test_binomial_skill_no_data() -> None:
    assert _binomial_skill(0, 0) == (0.0, 0.0)


def test_binomial_skill_50_50_is_half() -> None:
    likelihood, z = _binomial_skill(wins=10, resolved=20)
    assert abs(likelihood - 0.5) < 1e-6
    assert abs(z) < 1e-6


def test_binomial_skill_strong_signal() -> None:
    # 18/20 = 90% win rate ~ z=3.58, p ~ 0.9998
    likelihood, z = _binomial_skill(wins=18, resolved=20)
    assert likelihood > 0.99
    assert z > 3.0


def test_binomial_skill_below_50_returns_low_score() -> None:
    likelihood, z = _binomial_skill(wins=5, resolved=20)
    assert likelihood < 0.1
    assert z < -2.0


# ── _market_winning_outcome ──────────────────────────────────────────────────

def test_open_market_has_no_winner() -> None:
    # Open market with mid-range prices — not yet resolved.
    open_market = PolymarketMarket(
        gamma_id="x", condition_id="0xc", slug="", question="",
        category="", end_date="", outcomes=["Yes", "No"],
        outcome_prices=[0.6, 0.4],  # still trading
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
    assert e.stddevs_above_expected > 0


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


# ── compute_all_enrichment ────────────────────────────────────────────────────

def test_compute_all_buckets_by_wallet_and_attaches_name() -> None:
    activity = [
        PolymarketActivity(
            proxy_wallet="0xtheo",
            timestamp=1, condition_id="0xc1", type="TRADE",
            side="BUY", size=10, usdc_size=4, price=0.4,
            outcome_index=0, outcome="Yes", slug="", title="", event_slug="",
            transaction_hash="0xa",
        ),
        PolymarketActivity(
            proxy_wallet="0xfredi",
            timestamp=1, condition_id="0xc1", type="TRADE",
            side="BUY", size=10, usdc_size=6, price=0.6,
            outcome_index=1, outcome="No", slug="", title="", event_slug="",
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


def test_compute_all_sorts_by_skill_descending() -> None:
    # Wallet A: 3-0; wallet B: 0-3 — A should sort first.
    def trades_for(wallet: str, outcome: int) -> list[PolymarketActivity]:
        return [
            PolymarketActivity(
                proxy_wallet=wallet, timestamp=i, condition_id=f"0xc{i}",
                type="TRADE", side="BUY", size=10, usdc_size=5, price=0.5,
                outcome_index=outcome, outcome="", slug="", title="", event_slug="",
                transaction_hash=f"0x{wallet}{i}",
            ) for i in range(3)
        ]

    activity = trades_for("0xa", outcome=0) + trades_for("0xb", outcome=1)
    markets = [_market(f"0xc{i}", closed=True, winning_outcome=0) for i in range(3)]
    out = compute_all_enrichment(activity=activity, markets=markets, leaderboard=[])
    assert out[0].proxy_wallet == "0xa"
    assert out[-1].proxy_wallet == "0xb"
