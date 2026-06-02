"""
On-chain skill scoring for Polymarket wallets.

Approach
--------
For each wallet, walk its activity stream and group TRADE events by
(condition_id, outcome_index). Each group becomes a "position" with a
volume-weighted average entry price (the market's implied probability
that the picked outcome would resolve YES, at the moment the wallet
took the position). We then join against the markets index to find
which positions are on RESOLVED markets, and use the market's
outcome_prices to determine win/loss:

  - outcome_prices[outcome_index] == 1.0  → that outcome won → bet won
  - outcome_prices[outcome_index] == 0.0  → that outcome lost → bet lost

A "resolved bet" is one with a non-zero net_size at the time of
resolution. (Positions fully exited before resolution don't count as
bets — the wallet took realized P&L but the on-chain resolution
doesn't apply to them.)

Skill score: hierarchical Bayesian logistic edge model fit jointly
across all wallets in the enrichment pass.

    y_i ~ Bernoulli(q_i),     logit(q_i) = logit(p_i) + edge_w
    edge_w ~ Normal(mu_pop, sigma2_pop)        (empirical Bayes)

`skill_likelihood` is `P(edge_w > 0 | data)` — keeping the field name
for API back-compat but with semantics now keyed off the market's
implied price, not a 50/50 baseline. See bayesian_skill.py for the
derivation.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, replace

from .bayesian_skill import (
    Bet,
    PopulationPrior,
    PosteriorFit,
    effective_sample_size,
    fit_population_prior,
    fit_wallet_posterior,
    rank_score,
)
from .models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketWalletEnrichment,
    PolymarketWalletHydration,
)

log = logging.getLogger("marketsignalos.polymarket.skill")


# ── Position aggregation ──────────────────────────────────────────────────────

@dataclass(slots=True)
class _Position:
    """Net position in one (condition_id, outcome_index) leg."""

    condition_id: str
    outcome_index: int
    net_size: float = 0.0         # signed: + long, - short (Polymarket doesn't short, so always >= 0 net)
    total_cost_usdc: float = 0.0  # cumulative USDC spent buying (net of sells)
    realized_pnl_usdc: float = 0.0  # locked-in PnL from sells before resolution
    last_ts: int = 0
    event_slug: str = ""          # captured from the first matching TRADE event


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
        if not pos.event_slug and event.event_slug:
            pos.event_slug = event.event_slug

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


# ── Per-wallet rollup (bets + counting stats) ────────────────────────────────

def _market_winning_outcome(market: PolymarketMarket) -> int | None:
    """
    Inspect a market's outcome_prices to determine the winning outcome index.
    Returns None if the market isn't actually resolved.

    Canonical settlement is intentionally strict. Live markets frequently
    trade at 99c, so price alone is never sufficient evidence of resolution.
    """
    if not market.closed or market.active or not market.outcome_prices:
        return None
    winners = [i for i, price in enumerate(market.outcome_prices) if price >= 0.99]
    return winners[0] if len(winners) == 1 else None


def _apply_event_weights(bets: list[Bet]) -> list[Bet]:
    """Cap each event at one likelihood vote, split across legs by capital."""
    grouped: dict[str, list[tuple[int, Bet]]] = defaultdict(list)
    for index, bet in enumerate(bets):
        key = bet.event_slug or f"__blank_event_{index}"
        grouped[key].append((index, bet))

    weighted: list[Bet | None] = [None] * len(bets)
    for rows in grouped.values():
        total_cost = sum(max(0.0, bet.cost_usdc) for _, bet in rows)
        even_weight = 1.0 / len(rows)
        for index, bet in rows:
            weight = (
                max(0.0, bet.cost_usdc) / total_cost
                if total_cost > 0.0
                else even_weight
            )
            weighted[index] = replace(bet, weight=weight)
    return [bet for bet in weighted if bet is not None]


@dataclass(slots=True)
class _WalletRollup:
    """
    Intermediate per-wallet aggregation: counting stats + the list of
    resolved bets used as the likelihood for the Bayesian fit. Combined
    with a fitted PosteriorFit, this becomes a PolymarketWalletEnrichment.
    """

    wallet: str
    name: str
    pseudonym: str
    bets: list[Bet]
    wins: int
    losses: int
    total_pnl_usdc: float
    total_volume_usdc: float
    resolved_volume_usdc: float
    trade_count: int
    last_activity_ts: int
    position_sizes: list[float]


def _roll_up_wallet(
    wallet: str,
    activity: list[PolymarketActivity],
    markets_by_condition: dict[str, PolymarketMarket],
    *,
    name: str = "",
    pseudonym: str = "",
) -> _WalletRollup:
    """
    Walk one wallet's activity stream and produce the materials the
    Bayesian fit needs: a list of resolved Bet observations, plus the
    usual scalar counters for display.
    """
    positions = _aggregate_positions(activity)
    bets: list[Bet] = []
    wins = 0
    losses = 0
    total_pnl = 0.0
    total_volume = 0.0
    resolved_volume = 0.0
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

    for (_cond, outcome_idx), pos in positions.items():
        market = markets_by_condition.get(pos.condition_id)
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
        bet_cost = pos.total_cost_usdc
        # Volume-weighted average entry price = market-implied probability
        # of the picked outcome at the moment(s) the wallet bought.
        entry_price = bet_cost / pos.net_size if pos.net_size > 0 else 0.0
        won = (outcome_idx == winner)
        bets.append(
            Bet(
                entry_price=entry_price,
                won=won,
                event_slug=pos.event_slug,
                cost_usdc=bet_cost,
            )
        )
        position_sizes.append(bet_cost)
        resolved_volume += bet_cost

        if won:
            wins += 1
            unrealized = pos.net_size * 1.0 - pos.total_cost_usdc
        else:
            losses += 1
            unrealized = -pos.total_cost_usdc
        total_pnl += pos.realized_pnl_usdc + unrealized

    return _WalletRollup(
        wallet=wallet.lower(),
        name=name,
        pseudonym=pseudonym,
        bets=_apply_event_weights(bets),
        wins=wins,
        losses=losses,
        total_pnl_usdc=total_pnl,
        total_volume_usdc=total_volume,
        resolved_volume_usdc=resolved_volume,
        trade_count=trade_count,
        last_activity_ts=last_activity_ts,
        position_sizes=position_sizes,
    )


def _enrichment_from_rollup(
    rollup: _WalletRollup,
    fit: PosteriorFit,
    *,
    ess: float,
    hydration: PolymarketWalletHydration | None = None,
) -> PolymarketWalletEnrichment:
    resolved_trades = rollup.wins + rollup.losses
    win_rate = rollup.wins / resolved_trades if resolved_trades > 0 else 0.0
    avg_size = (
        sum(rollup.position_sizes) / len(rollup.position_sizes)
        if rollup.position_sizes
        else 0.0
    )
    rs = rank_score(
        posterior_skill=fit.posterior_skill,
        edge_lower_bound=fit.edge_lower_bound,
        resolved_volume_usdc=rollup.resolved_volume_usdc,
    )

    has_hydration = hydration is not None
    hydration = hydration or PolymarketWalletHydration(proxy_wallet=rollup.wallet)
    all_time_volume = (
        hydration.all_time_volume_usdc if has_hydration else rollup.total_volume_usdc
    )
    all_time_pnl = hydration.all_time_pnl_usdc if has_hydration else rollup.total_pnl_usdc
    all_time_roi = all_time_pnl / all_time_volume if all_time_volume > 0 else 0.0
    data_reasons: list[str] = []
    if not hydration.activity_history_complete:
        data_reasons.append("incomplete activity history")
    if not hydration.positions_complete:
        data_reasons.append("incomplete current positions")
    if not hydration.closed_positions_complete:
        data_reasons.append("incomplete closed positions")
    if not hydration.economic_all_time_complete:
        data_reasons.append("missing all-time economics")
    if not hydration.economic_month_complete:
        data_reasons.append("missing 30-day economics")
    if hydration.metadata_coverage < 1.0:
        data_reasons.append("incomplete market metadata")
    data_status = "trusted" if not data_reasons else "untrusted"

    economic_qualified = (
        all_time_pnl > 0.0
        and all_time_roi > 0.0
        and hydration.pnl_30d_usdc >= 0.0
    )
    tailability_reasons = list(data_reasons)
    if ess < 20.0:
        tailability_reasons.append("fewer than 20 independent settled events")
    if fit.posterior_skill < 0.80:
        tailability_reasons.append("forecast confidence below 80%")
    if fit.edge_lower_bound <= 0.0:
        tailability_reasons.append("conservative edge is not positive")
    if all_time_pnl <= 0.0:
        tailability_reasons.append("negative or zero all-time PnL")
    if all_time_roi <= 0.0:
        tailability_reasons.append("negative or zero all-time ROI")
    if hydration.pnl_30d_usdc < 0.0:
        tailability_reasons.append("recent PnL below zero")
    tailability_status = "tailable" if not tailability_reasons else "blocked"

    return PolymarketWalletEnrichment(
        proxy_wallet=rollup.wallet,
        name=rollup.name,
        pseudonym=rollup.pseudonym,
        resolved_trades=resolved_trades,
        wins=rollup.wins,
        losses=rollup.losses,
        win_rate=round(win_rate, 6),
        skill_likelihood=round(fit.posterior_skill, 6),
        stddevs_above_expected=round(fit.edge_z, 4),
        edge_mean=round(fit.edge_mean, 6),
        edge_lower_bound=round(fit.edge_lower_bound, 6),
        effective_sample_size=round(ess, 4),
        resolved_volume_usdc=round(rollup.resolved_volume_usdc, 2),
        rank_score=round(rs, 6),
        total_volume_usdc=round(all_time_volume, 2),
        total_pnl_usdc=round(all_time_pnl, 2),
        avg_position_size_usdc=round(avg_size, 2),
        trade_count=rollup.trade_count,
        last_activity_at=rollup.last_activity_ts,
        forecast_skill_likelihood=round(fit.posterior_skill, 6),
        forecast_edge_mean=round(fit.edge_mean, 6),
        forecast_edge_lower_bound=round(fit.edge_lower_bound, 6),
        independent_settled_events=round(ess, 4),
        all_time_pnl_usdc=round(all_time_pnl, 2),
        all_time_volume_usdc=round(all_time_volume, 2),
        all_time_roi=round(all_time_roi, 8),
        pnl_30d_usdc=round(hydration.pnl_30d_usdc, 2),
        active_pnl_usdc=round(hydration.active_pnl_usdc, 2),
        max_drawdown_usdc=round(hydration.max_drawdown_usdc, 2),
        data_quality_status=data_status,
        data_quality_reasons=data_reasons,
        economic_qualified=economic_qualified,
        tailability_status=tailability_status,
        tailability_reasons=tailability_reasons,
    )


# ── Public API ───────────────────────────────────────────────────────────────

def compute_wallet_enrichment(
    wallet: str,
    *,
    activity: list[PolymarketActivity],
    markets_by_condition: dict[str, PolymarketMarket],
    name: str = "",
    pseudonym: str = "",
    population_prior: PopulationPrior | None = None,
    hydration: PolymarketWalletHydration | None = None,
) -> PolymarketWalletEnrichment:
    """
    Compute one wallet's enrichment row.

    `population_prior` is the (mu, sigma2) Empirical-Bayes hyperprior
    used to shrink edge estimates. When omitted (single-wallet callers,
    tests) a weakly-informative N(0, 0.25) is used so that a wallet
    with 5 wins / 5 losses on 50¢ markets still posts a near-zero edge
    rather than a sample-mean-driven extreme value.
    """
    rollup = _roll_up_wallet(
        wallet,
        activity,
        markets_by_condition,
        name=name,
        pseudonym=pseudonym,
    )
    prior = population_prior or PopulationPrior(mu=0.0, sigma2=0.25)
    fit = fit_wallet_posterior(
        rollup.bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2
    )
    ess = effective_sample_size(rollup.bets)
    return _enrichment_from_rollup(rollup, fit, ess=ess, hydration=hydration)


def compute_all_enrichment(
    *,
    activity: list[PolymarketActivity],
    markets: list[PolymarketMarket],
    leaderboard: list[PolymarketLeaderboardEntry],
    hydration_by_wallet: dict[str, PolymarketWalletHydration] | None = None,
) -> list[PolymarketWalletEnrichment]:
    """
    Compute enrichment for every wallet present in the activity stream.

    The leaderboard list is used purely to look up display names /
    pseudonyms (since the data-api uses the pseudonymized
    'Ironclad-Tenement' style, but lb-api uses the canonical 'Theo4'
    name).

    The Bayesian fit runs in two passes:
      1. Roll up every wallet's bets, then fit Empirical-Bayes
         (mu_pop, sigma2_pop) from per-wallet MLEs.
      2. Refit each wallet under the empirical prior — this is the
         shrinkage step that prevents 5/5 wallets from looking elite.
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

    # Pass 1: roll up every wallet so we have the bet lists in hand.
    rollups: list[_WalletRollup] = []
    for wallet, events in by_wallet.items():
        name, pseudonym = names_by_wallet.get(wallet, ("", ""))
        rollups.append(
            _roll_up_wallet(
                wallet, events, markets_by_condition,
                name=name, pseudonym=pseudonym,
            )
        )

    # Empirical-Bayes population prior from per-wallet MLEs.
    prior = fit_population_prior([r.bets for r in rollups])

    # Pass 2: fit each wallet under the empirical prior.
    out: list[PolymarketWalletEnrichment] = []
    for rollup in rollups:
        fit = fit_wallet_posterior(
            rollup.bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2
        )
        ess = effective_sample_size(rollup.bets)
        hydration = (hydration_by_wallet or {}).get(rollup.wallet)
        out.append(_enrichment_from_rollup(rollup, fit, ess=ess, hydration=hydration))

    # Default order: best-ranked first. Tiebreak by skill_likelihood then
    # wallet for determinism.
    out.sort(
        key=lambda e: (-e.rank_score, -e.skill_likelihood, e.proxy_wallet)
    )
    log.info(
        "enrichment_computed wallets=%d markets=%d resolved_bets=%d "
        "prior=(mu=%.3f, sigma2=%.3f)",
        len(out),
        len(markets_by_condition),
        sum(e.resolved_trades for e in out),
        prior.mu,
        prior.sigma2,
    )
    return out
