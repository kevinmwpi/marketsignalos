from __future__ import annotations

from datetime import UTC, datetime

from marketsignalos_polymarket.price_lead import (
    LeadBet,
    compute_price_lead,
    parse_iso_ts,
)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _bet(
    cond: str,
    *,
    entry_price: float = 0.40,
    entry_ts: int = 1_000_000,
    won: bool | None = None,
    outcome_index: int = 0,
    market_end_ts: int = 0,
    cost: float = 100.0,
) -> LeadBet:
    return LeadBet(
        condition_id=cond,
        outcome_index=outcome_index,
        event_slug=f"event-{cond}",
        entry_price=entry_price,
        cost_usdc=cost,
        entry_ts=entry_ts,
        won=won,
        market_end_ts=market_end_ts,
    )


def test_parse_iso_ts_handles_z_suffix_and_blank() -> None:
    assert parse_iso_ts("1970-01-01T01:00:00Z") == 3600
    assert parse_iso_ts("1970-01-01T01:00:00+00:00") == 3600
    assert parse_iso_ts("") == 0
    assert parse_iso_ts("not-a-date") == 0


def test_post_entry_drift_scores_market_following_entries() -> None:
    """Three independent bets whose picked side gains 10¢ within an hour of
    entry — the maximal drift sub-score."""
    bets = [_bet(f"0xc{i}") for i in range(3)]
    history = {
        f"0xc{i}": [(_iso(1_000_000 + 3600), 0.50, False)] for i in range(3)
    }
    lead = compute_price_lead(bets, history)
    assert lead.post_entry_drift_48h == 0.1
    assert lead.post_entry_drift_samples == 3.0
    assert lead.price_lead_score == 1.0
    assert any("within 48h of entry" in d for d in lead.drivers)


def test_drift_uses_picked_side_for_no_bets() -> None:
    """A NO-side bet profits when the YES price falls: yes 0.60 → 0.50 means
    the picked (NO) side moved from 0.40 to 0.50."""
    bets = [_bet(f"0xc{i}", outcome_index=1) for i in range(3)]
    history = {
        f"0xc{i}": [(_iso(1_000_000 + 3600), 0.50, False)] for i in range(3)
    }
    lead = compute_price_lead(bets, history)
    assert lead.post_entry_drift_48h == 0.1
    assert lead.price_lead_score == 1.0


def test_drift_ignores_observations_outside_window_and_closed_rows() -> None:
    bets = [_bet(f"0xc{i}") for i in range(3)]
    history = {
        # First row is post-close (resolution payoff, not opinion); second is
        # 3 days later — outside the 48h window.
        f"0xc{i}": [
            (_iso(1_000_000 + 1800), 0.99, True),
            (_iso(1_000_000 + 72 * 3600), 0.50, False),
        ]
        for i in range(3)
    }
    lead = compute_price_lead(bets, history)
    assert lead.post_entry_drift_samples == 0.0
    assert lead.price_lead_score == 0.0
    assert lead.drivers == ["insufficient price/resolution data to measure"]


def test_drift_needs_minimum_weighted_samples() -> None:
    bets = [_bet(f"0xc{i}") for i in range(2)]  # only 2 — below the floor of 3
    history = {
        f"0xc{i}": [(_iso(1_000_000 + 3600), 0.50, False)] for i in range(2)
    }
    lead = compute_price_lead(bets, history)
    assert lead.post_entry_drift_samples == 2.0
    assert lead.price_lead_score == 0.0


def test_longshot_conversion_scores_wins_above_implied() -> None:
    """Six resolved 20¢ entries with 3 wins: 50% realized vs 20% implied —
    far above the market's odds, maximal sub-score."""
    bets = [
        _bet(f"0xc{i}", entry_price=0.20, won=i < 3) for i in range(6)
    ]
    lead = compute_price_lead(bets, {})
    assert lead.longshot_events == 6.0
    assert lead.longshot_win_rate == 0.5
    assert lead.longshot_implied_rate == 0.2
    assert lead.price_lead_score == 1.0
    assert any("sub-25c entries" in d for d in lead.drivers)


def test_longshot_underperformance_scores_zero_not_missing() -> None:
    bets = [
        _bet(f"0xc{i}", entry_price=0.20, won=i < 1) for i in range(6)
    ]  # 1/6 ≈ 17% < 20% implied
    lead = compute_price_lead(bets, {})
    assert lead.longshot_events == 6.0
    assert lead.price_lead_score == 0.0
    # The sub-signal WAS measured — the driver reports the weak record.
    assert any("sub-25c entries" in d for d in lead.drivers)


def test_late_entry_accuracy_scores_wins_near_close() -> None:
    """Five resolved 50¢ bets entered 24h before close, 4 wins: 80% vs 50%
    implied — above the +25pt full-score excess."""
    end_ts = 2_000_000
    bets = [
        _bet(
            f"0xc{i}",
            entry_price=0.50,
            entry_ts=end_ts - 24 * 3600,
            won=i < 4,
            market_end_ts=end_ts,
        )
        for i in range(5)
    ]
    lead = compute_price_lead(bets, {})
    assert lead.late_entry_events == 5.0
    assert lead.late_entry_win_rate == 0.8
    assert lead.price_lead_score == 1.0
    assert any("within 72h of close" in d for d in lead.drivers)


def test_event_capping_prevents_one_event_dominating_longshots() -> None:
    """Five 20¢ legs of the SAME event count as one independent event —
    below the 5-event floor, so no longshot sub-score."""
    bets = [
        LeadBet(
            condition_id=f"0xc{i}",
            outcome_index=0,
            event_slug="one-big-event",
            entry_price=0.20,
            cost_usdc=100.0,
            entry_ts=1_000_000,
            won=True,
            market_end_ts=0,
        )
        for i in range(5)
    ]
    lead = compute_price_lead(bets, {})
    assert lead.longshot_events == 1.0
    assert lead.price_lead_score == 0.0


def test_no_usable_bets_is_unmeasured() -> None:
    lead = compute_price_lead([_bet("0xc0", entry_price=0.0)], {})
    assert lead.price_lead_score == 0.0
    assert lead.drivers == ["no bets with a usable entry price"]


def test_score_averages_available_sub_signals() -> None:
    """Longshot maxed (score 1.0) + late-entry at exactly implied (score 0)
    → mean 0.5. Drift unavailable (no history)."""
    end_ts = 2_000_000
    longshots = [
        _bet(f"0xls{i}", entry_price=0.20, won=i < 3) for i in range(6)
    ]
    late = [
        _bet(
            f"0xlate{i}",
            entry_price=0.50,
            entry_ts=end_ts - 3600,
            won=i < 3,  # 3/6 = exactly the 50% implied
            market_end_ts=end_ts,
        )
        for i in range(6)
    ]
    lead = compute_price_lead(longshots + late, {})
    assert lead.price_lead_score == 0.5
    assert len(lead.drivers) == 2
