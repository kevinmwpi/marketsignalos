"""
On-chain skill scoring for Polymarket wallets.

Approach
--------
For each wallet, walk its activity stream and group TRADE events by
(condition_id, outcome_index). This builds a position per "bet". We then
join against the markets index to find which bets are on RESOLVED markets,
and use the market's outcome_prices to determine win/loss:

  - outcome_prices[outcome_index] == 1.0  → that outcome won → position pays out
  - outcome_prices[outcome_index] == 0.0  → that outcome lost → position is worthless

A "resolved bet" is one with a non-zero net_size at the time of resolution.
(Positions fully exited before resolution don't count as bets — the wallet
took realized profit/loss but the resolution doesn't apply to them.)

Skill score: normal approximation of the binomial test against p=0.5:
    z = (wins - n*0.5) / sqrt(n*0.25)
    skill_likelihood = Phi(z)

This matches the existing Kalshi pipeline (services/.../skill_scoring.py)
so the two leaderboards are directly comparable.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass

from .models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketWalletEnrichment,
)

log = logging.getLogger("marketsignalos.polymarket.skill")


# ── Pure math helpers ─────────────────────────────────────────────────────────

def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _binomial_skill(wins: int, resolved: int) -> tuple[float, float]:
    """Returns (skill_likelihood, z_score) for a binomial test against p=0.5."""
    if resolved <= 0:
        return 0.0, 0.0
    expected = resolved * 0.5
    std = math.sqrt(resolved * 0.25)
    z = (wins - expected) / std
    return _normal_cdf(z), z


# ── Position aggregation ──────────────────────────────────────────────────────

@dataclass(slots=True)
class _Position:
    """Net position in one (condition_id, outcome_index) leg."""

    condition_id: str
    outcome_index: int
    net_size: float = 0.0       # signed: + long, - short (Polymarket doesn't short, so always >= 0 net)
    total_cost_usdc: float = 0.0  # cumulative USDC spent buying (net of sells)
    realized_pnl_usdc: float = 0.0  # locked-in PnL from sells before resolution
    last_ts: int = 0


def _aggregate_positions(activity: list[PolymarketActivity]) -> dict[tuple[str, int], _Position]:
    """
    Aggregate TRADE events into per-(condition, outcome) positions.

    BUY: increases net_size at the trade price, total_cost grows by usdc_size.
    SELL: decreases net_size; the cost basis of the sold shares is removed
          proportionally and the difference is realized PnL.

    REDEEM/MERGE/SPLIT events are ignored here — REDEEM is what tells us
    the position settled, but for the win/loss determination we use the
    market's outcome_prices directly. Tracking REDEEMs separately would
    double-count.
    """
    positions: dict[tuple[str, int], _Position] = {}

    # Activity arrives newest-first from the API. Process oldest-first so
    # avg cost is computed correctly.
    for event in sorted(activity, key=lambda e: e.timestamp):
        if event.type != "TRADE":
            continue
        if not event.condition_id:
            continue
        # outcomeIndex=999 means "no direction" — TRADE rows always have 0 or 1.
        if event.outcome_index not in (0, 1):
            continue

        key = (event.condition_id, event.outcome_index)
        pos = positions.get(key) or _Position(event.condition_id, event.outcome_index)

        side = event.side.upper()
        if side == "BUY":
            pos.net_size += event.size
            pos.total_cost_usdc += event.usdc_size
        elif side == "SELL":
            if pos.net_size > 0:
                # Proportional cost-basis removal.
                avg_cost = pos.total_cost_usdc / pos.net_size if pos.net_size > 0 else 0.0
                sold_basis = avg_cost * min(event.size, pos.net_size)
                pos.realized_pnl_usdc += event.usdc_size - sold_basis
                pos.total_cost_usdc -= sold_basis
                pos.net_size -= event.size
                if pos.net_size < 1e-9:
                    pos.net_size = 0.0
                    pos.total_cost_usdc = 0.0
            else:
                # Sell with no inventory — could be from a MERGE we ignored.
                # Skip to avoid corrupting cost basis.
                continue
        else:
            continue

        pos.last_ts = max(pos.last_ts, event.timestamp)
        positions[key] = pos

    return positions


# ── Per-wallet enrichment ─────────────────────────────────────────────────────

def _market_winning_outcome(market: PolymarketMarket) -> int | None:
    """
    Inspect a market's outcome_prices to determine the winning outcome index.
    Returns None if the market isn't actually resolved.

    Note: Gamma's `closed` boolean is unreliable (markets with future end
    dates can be marked closed=True). The authoritative signal is
    outcome_prices having a definite winner (>= 0.99), which corresponds to
    UMA having settled the market on-chain.
    """
    if not market.outcome_prices:
        return None
    # A resolved market has one outcome at 1.0 (winner) and the others at 0.0.
    # Use >= 0.99 to tolerate float rounding.
    for i, price in enumerate(market.outcome_prices):
        if price >= 0.99:
            return i
    return None  # still trading / canceled / undetermined


def compute_wallet_enrichment(
    wallet: str,
    *,
    activity: list[PolymarketActivity],
    markets_by_condition: dict[str, PolymarketMarket],
    name: str = "",
    pseudonym: str = "",
) -> PolymarketWalletEnrichment:
    """
    Compute one wallet's enrichment row from its activity stream and the
    markets index.
    """
    positions = _aggregate_positions(activity)

    wins = 0
    losses = 0
    total_pnl = 0.0
    total_volume = 0.0
    trade_count = 0
    last_activity_ts = 0
    position_sizes: list[float] = []

    # Volume + trade counts span the full activity stream regardless of resolution.
    for event in activity:
        if event.type == "TRADE":
            trade_count += 1
            total_volume += event.usdc_size
        if event.timestamp > last_activity_ts:
            last_activity_ts = event.timestamp

    # Win/loss only counts positions on resolved markets with non-zero net size.
    for (cond, outcome_idx), pos in positions.items():
        market = markets_by_condition.get(cond)
        if market is None:
            total_pnl += pos.realized_pnl_usdc
            continue

        winner = _market_winning_outcome(market)
        if winner is None:
            # Market hasn't resolved yet, or was canceled — exclude from win/loss.
            total_pnl += pos.realized_pnl_usdc
            continue

        if pos.net_size <= 1e-9:
            # Fully exited before resolution — count only realized PnL, no win/loss.
            total_pnl += pos.realized_pnl_usdc
            continue

        # This is a resolved bet.
        bet_size_usdc = pos.total_cost_usdc
        position_sizes.append(bet_size_usdc)

        if outcome_idx == winner:
            wins += 1
            # Each share pays out $1 if your outcome wins; PnL = shares - cost.
            unrealized = pos.net_size * 1.0 - pos.total_cost_usdc
        else:
            losses += 1
            # Position expired worthless.
            unrealized = -pos.total_cost_usdc

        total_pnl += pos.realized_pnl_usdc + unrealized

    resolved_trades = wins + losses
    win_rate = wins / resolved_trades if resolved_trades > 0 else 0.0
    skill_likelihood, z_score = _binomial_skill(wins, resolved_trades)
    avg_position_size = (
        sum(position_sizes) / len(position_sizes) if position_sizes else 0.0
    )

    return PolymarketWalletEnrichment(
        proxy_wallet=wallet.lower(),
        name=name,
        pseudonym=pseudonym,
        resolved_trades=resolved_trades,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 6),
        skill_likelihood=round(skill_likelihood, 6),
        stddevs_above_expected=round(z_score, 4),
        total_volume_usdc=round(total_volume, 2),
        total_pnl_usdc=round(total_pnl, 2),
        avg_position_size_usdc=round(avg_position_size, 2),
        trade_count=trade_count,
        last_activity_at=last_activity_ts,
    )


# ── Batch driver ──────────────────────────────────────────────────────────────

def compute_all_enrichment(
    *,
    activity: list[PolymarketActivity],
    markets: list[PolymarketMarket],
    leaderboard: list[PolymarketLeaderboardEntry],
) -> list[PolymarketWalletEnrichment]:
    """
    Compute enrichment for every wallet present in the activity stream.

    The leaderboard list is used purely to look up display names/pseudonyms
    (since the data-api uses the pseudonymized 'Ironclad-Tenement' style,
    but lb-api uses the canonical 'Theo4' name).
    """
    # Bucket activity by wallet.
    by_wallet: dict[str, list[PolymarketActivity]] = defaultdict(list)
    for event in activity:
        by_wallet[event.proxy_wallet.lower()].append(event)

    # Build the markets index — keep the most recently fetched per condition_id.
    markets_by_condition: dict[str, PolymarketMarket] = {}
    for market in markets:
        existing = markets_by_condition.get(market.condition_id)
        if existing is None or market.fetched_at > existing.fetched_at:
            markets_by_condition[market.condition_id] = market

    # Display-name lookup from leaderboard.
    names_by_wallet: dict[str, tuple[str, str]] = {}
    for entry in leaderboard:
        if entry.proxy_wallet not in names_by_wallet:
            names_by_wallet[entry.proxy_wallet] = (entry.name, entry.pseudonym)

    out: list[PolymarketWalletEnrichment] = []
    for wallet, events in by_wallet.items():
        name, pseudonym = names_by_wallet.get(wallet, ("", ""))
        out.append(
            compute_wallet_enrichment(
                wallet,
                activity=events,
                markets_by_condition=markets_by_condition,
                name=name,
                pseudonym=pseudonym,
            )
        )

    out.sort(
        key=lambda e: (-e.skill_likelihood, -e.resolved_trades, e.proxy_wallet)
    )
    log.info(
        "enrichment_computed wallets=%d markets=%d resolved_bets=%d",
        len(out),
        len(markets_by_condition),
        sum(e.resolved_trades for e in out),
    )
    return out
