from __future__ import annotations

from marketsignalos_polymarket.models import PolymarketActivity
from marketsignalos_polymarket.skill_computation import compute_wallet_enrichment
from marketsignalos_polymarket.trader_style import (
    DISCRETIONARY_THRESHOLD,
    SYSTEMATIC_THRESHOLD,
    compute_trader_style,
)


def _trade(
    cond: str,
    *,
    ts: int,
    side: str = "BUY",
    size: float = 10.0,
    price: float = 0.5,
    outcome: int = 0,
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
        event_slug=f"event-{cond}",
        transaction_hash=f"0x{ts}{cond}{side}",
    )


def _systematic_stream() -> list[PolymarketActivity]:
    """288 uniform-size buys every 10 minutes for ~48h across 40 markets —
    the reference-bot shape: fast cadence, 24h coverage, repeated sizing."""
    return [
        _trade(f"0xc{i % 40:02d}", ts=1_000_000 + i * 600) for i in range(288)
    ]


def _discretionary_stream() -> list[PolymarketActivity]:
    """One varied-size buy per day at the same hour for 60 days across 20
    markets — a human-paced pattern."""
    return [
        _trade(f"0xc{i // 3:02d}", ts=2_000_000 + i * 86_400, size=10.0 + i)
        for i in range(60)
    ]


def test_systematic_profile_classified() -> None:
    style = compute_trader_style(_systematic_stream())
    assert style.archetype == "systematic"
    assert style.automation_score >= SYSTEMATIC_THRESHOLD
    assert style.trades_per_active_day > 50.0
    assert style.median_intertrade_gap_seconds == 600.0
    assert style.active_utc_hours == 24
    assert style.buy_size_uniformity == 1.0
    # Every available sub-signal contributes one human-readable driver.
    assert len(style.drivers) == 5
    assert any("trades per active day" in d for d in style.drivers)
    assert any("between trades" in d for d in style.drivers)
    assert any("/24 UTC hours" in d for d in style.drivers)


def test_discretionary_profile_classified() -> None:
    style = compute_trader_style(_discretionary_stream())
    assert style.archetype == "discretionary"
    assert style.automation_score <= DISCRETIONARY_THRESHOLD
    assert style.active_utc_hours == 1
    assert style.buy_size_uniformity < 0.1
    assert style.median_intertrade_gap_seconds == 86_400.0


def test_insufficient_history_is_unclassified() -> None:
    style = compute_trader_style(_systematic_stream()[:10])
    assert style.archetype == "unclassified"
    assert style.automation_score == 0.0
    assert any("insufficient history" in d for d in style.drivers)
    # Raw metrics are still reported even without a label.
    assert style.trades_per_active_day > 0.0


def test_no_trades_is_unclassified() -> None:
    redeem = PolymarketActivity(
        proxy_wallet="0xabc",
        timestamp=1_000_000,
        condition_id="0xc1",
        type="REDEEM",
        side="",
        size=10.0,
        usdc_size=10.0,
        price=1.0,
        outcome_index=0,
        outcome="Yes",
        slug="",
        title="",
        event_slug="",
        transaction_hash="0xr",
    )
    style = compute_trader_style([redeem])
    assert style.archetype == "unclassified"
    assert style.drivers == ["no trades observed"]


def test_category_and_exit_descriptors() -> None:
    activity = [
        _trade("0xc1", ts=1_000_000),
        _trade("0xc1", ts=1_001_000),
        _trade("0xc1", ts=1_002_000, side="SELL", size=20.0),  # full exit after 2000s
        _trade("0xc2", ts=1_003_000),
    ]
    style = compute_trader_style(
        activity,
        categories_by_condition={"0xc1": "Sports", "0xc2": "Politics"},
    )
    assert style.top_category == "Sports"
    assert style.top_category_share == 0.75
    # One of two opened positions fully exited; held first BUY -> last SELL.
    assert style.exited_position_share == 0.5
    assert style.median_exit_hold_hours == round(2000 / 3600, 2)


def test_style_is_deterministic() -> None:
    assert compute_trader_style(_systematic_stream()) == compute_trader_style(
        _systematic_stream()
    )


def test_enrichment_carries_style_fields() -> None:
    enrichment = compute_wallet_enrichment(
        "0xabc",
        activity=_systematic_stream(),
        markets_by_condition={},
    )
    assert enrichment.style_archetype == "systematic"
    assert enrichment.automation_score >= SYSTEMATIC_THRESHOLD
    assert enrichment.style_drivers
    assert enrichment.trades_per_active_day > 50.0
    assert enrichment.active_utc_hours == 24
