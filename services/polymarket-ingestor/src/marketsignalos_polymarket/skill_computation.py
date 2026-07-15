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
import math
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from .bayesian_skill import (
    Bet,
    PopulationPrior,
    PosteriorFit,
    apply_recency_weights,
    effective_sample_size,
    fit_wallet_posterior,
    population_prior_from_fits,
    rank_score,
    weak_prior_fit,
)
from .models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPriceSnapshot,
    PolymarketWalletBet,
    PolymarketWalletEnrichment,
    PolymarketWalletHydration,
)
from .price_lead import (
    LeadBet,
    PriceLead,
    compute_price_lead,
    parse_iso_ts,
    unmeasured_price_lead,
)
from .trader_style import TraderStyle, compute_trader_style, unclassified_style

log = logging.getLogger("marketsignalos.polymarket.skill")

# Tailability floor for the recency-weighted fit: a wallet must have at least
# this many *recent* independent settled events (after exponential decay) to
# remain tailable. Protects against tailing a wallet whose record is entirely
# stale — the lifetime gate (ESS >= 20) can't catch that.
MIN_RECENT_INDEPENDENT_EVENTS = 5.0

# Minimum closing-line-value observations before a wallet's CLV edge counts as
# tailability evidence. CLV — did the wallet get a better price than where the
# market settled? — is the cleanest, market-mechanism-independent measure of
# edge, so it gates tailability rather than merely decorating the row.
MIN_CLV_SAMPLE = 10.0

# Floor for trusting a per-category edge fit over the wallet-level score.
# Below this the category posterior is dominated by its prior (the wallet's
# lifetime fit), so it adds no information — consumers fall back.
CATEGORY_MIN_INDEPENDENT_EVENTS = 5.0


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
    # Buys-only accumulators — never reduced by sells, so the original
    # volume-weighted entry price survives a full exit (CLV needs it).
    bought_size: float = 0.0
    bought_cost_usdc: float = 0.0


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
            pos.bought_size += event.size
            pos.bought_cost_usdc += event.usdc_size
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

    Settlement requires Gamma's closed flag AND a unique >=0.99 winner. Live
    markets frequently trade at 99c, so price alone is never sufficient
    evidence of resolution. The active flag carries no settlement signal:
    Gamma keeps active=True on resolved markets, so requiring active=False
    would classify every market as unresolved.
    """
    if not market.closed or not market.outcome_prices:
        return None
    winners = [i for i, price in enumerate(market.outcome_prices) if price >= 0.99]
    return winners[0] if len(winners) == 1 else None


def _event_capped_weights(items: list[tuple[str, float]]) -> list[float]:
    """
    One total vote per event, split across that event's legs by at-risk
    capital. `items` is (event_slug, cost_usdc) per observation; blank slugs
    are treated as independent events.
    """
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, (event_slug, _cost) in enumerate(items):
        grouped[event_slug or f"__blank_event_{index}"].append(index)

    weights = [0.0] * len(items)
    for indexes in grouped.values():
        total_cost = sum(max(0.0, items[i][1]) for i in indexes)
        even_weight = 1.0 / len(indexes)
        for i in indexes:
            weights[i] = (
                max(0.0, items[i][1]) / total_cost if total_cost > 0.0 else even_weight
            )
    return weights


def _apply_event_weights(bets: list[Bet]) -> list[Bet]:
    """Cap each event at one likelihood vote, split across legs by capital."""
    weights = _event_capped_weights([(b.event_slug, b.cost_usdc) for b in bets])
    return [replace(bet, weight=weight) for bet, weight in zip(bets, weights)]


# ── Per-category edge decomposition ──────────────────────────────────────────

def _normalize_category(raw: str) -> str:
    """Gamma category strings vary in case; group on a canonical form."""
    return raw.strip().lower()


def _category_edges(
    bets: list[Bet], lifetime_fit: PosteriorFit
) -> list[dict[str, Any]]:
    """
    Partial-pooling per-category refit: the same Bayesian edge model run on
    each category's bets with the wallet's OWN lifetime posterior as the
    prior. A category with plenty of data moves away from the wallet-level
    estimate; a thin one shrinks back to it (which is exactly the fallback
    consumers want). Bets on markets without a Gamma category are skipped —
    they still shape the lifetime fit that anchors every category prior.
    """
    grouped: dict[str, list[Bet]] = defaultdict(list)
    for bet in bets:
        if bet.category:
            grouped[bet.category].append(bet)

    out: list[dict[str, Any]] = []
    for category, category_bets in grouped.items():
        fit = fit_wallet_posterior(
            category_bets,
            mu_prior=lifetime_fit.edge_mean,
            # Guard against a degenerate zero-variance prior; the lifetime
            # Laplace variance is strictly positive in practice.
            sigma2_prior=max(lifetime_fit.edge_var, 1e-6),
        )
        out.append(
            {
                "category": category,
                "skill_likelihood": round(fit.posterior_skill, 6),
                "edge_mean": round(fit.edge_mean, 6),
                "edge_lower_bound": round(fit.edge_lower_bound, 6),
                "independent_events": round(
                    effective_sample_size(category_bets), 4
                ),
            }
        )
    out.sort(key=lambda row: (-float(row["independent_events"]), str(row["category"])))
    return out


# ── Price history & closing-line value ───────────────────────────────────────

# condition_id -> [(fetched_at, yes_price, closed)] sorted by fetched_at.
_PriceHistory = dict[str, list[tuple[str, float, bool]]]


def _market_price(market: PolymarketMarket) -> float:
    """Best available YES price for one market row: last trade, else mid."""
    if market.last_trade_price is not None and market.last_trade_price > 0.0:
        return market.last_trade_price
    if (
        market.best_bid is not None
        and market.best_ask is not None
        and market.best_bid > 0.0
        and market.best_ask > 0.0
    ):
        return (market.best_bid + market.best_ask) / 2.0
    return 0.0


def build_price_history(
    markets: list[PolymarketMarket],
    snapshots: list[PolymarketPriceSnapshot] | None = None,
) -> _PriceHistory:
    """
    Merge the appended price-snapshot stream with the (deduplicated) market
    rows into one per-condition price series. Zero-price points are dropped —
    they mean "no price observed", not "price is zero".
    """
    history: _PriceHistory = defaultdict(list)
    for snap in snapshots or []:
        if snap.yes_price > 0.0:
            history[snap.condition_id].append(
                (snap.fetched_at, snap.yes_price, snap.closed)
            )
    for market in markets:
        price = _market_price(market)
        if price > 0.0:
            history[market.condition_id].append(
                (market.fetched_at, price, market.closed)
            )
    for series in history.values():
        series.sort()
    return dict(history)


def _closing_line(
    history: _PriceHistory, condition_id: str, *, resolved: bool
) -> float | None:
    """
    Reference YES price a bet's entry is compared against.

    Resolved markets: the last price observed while the market was still
    open — the closing line. (Post-close prices are resolution payoffs, not
    market opinion.) Unresolved markets: the latest observed price.
    Returns None when no usable observation exists.
    """
    series = history.get(condition_id)
    if not series:
        return None
    if resolved:
        for fetched_at, price, closed in reversed(series):
            if not closed:
                return price
        return None
    return series[-1][1]


def _weighted_clv_stats(
    observations: list[tuple[float, str, float]],
) -> tuple[float, float, float]:
    """
    (clv_mean, clv_lower_bound, clv_sample_size) from (clv, event_slug,
    cost_usdc) observations. Event-capped weights; the lower bound is the
    normal-approximation 5th percentile of the weighted mean.
    """
    if not observations:
        return 0.0, 0.0, 0.0
    weights = _event_capped_weights([(slug, cost) for _, slug, cost in observations])
    total_weight = sum(weights)
    if total_weight <= 0.0:
        return 0.0, 0.0, 0.0
    mean = sum(w * clv for w, (clv, _, _) in zip(weights, observations)) / total_weight
    variance = (
        sum(w * (clv - mean) ** 2 for w, (clv, _, _) in zip(weights, observations))
        / total_weight
    )
    sum_sq = sum(w * w for w in weights)
    n_eff = (total_weight * total_weight) / sum_sq if sum_sq > 0.0 else 0.0
    se = math.sqrt(variance / n_eff) if n_eff > 0.0 else 0.0
    return mean, mean - 1.6448536269514722 * se, total_weight


@dataclass(slots=True)
class _PositionRecord:
    """One (condition, outcome) betting record — open, exited, or resolved."""

    condition_id: str
    outcome_index: int
    event_slug: str
    status: str  # "won" | "lost" | "exited" | "open"
    entry_price: float        # buys-only VWAP
    cost_usdc: float          # total USDC bought into the position
    net_size: float
    realized_pnl_usdc: float
    total_pnl_usdc: float
    market_resolved: bool
    last_ts: int


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
    records: list[_PositionRecord]


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

    records: list[_PositionRecord] = []

    def _record(pos: _Position, outcome_idx: int, status: str, pnl: float,
                *, market_resolved: bool) -> None:
        records.append(
            _PositionRecord(
                condition_id=pos.condition_id,
                outcome_index=outcome_idx,
                event_slug=pos.event_slug,
                status=status,
                entry_price=(
                    pos.bought_cost_usdc / pos.bought_size
                    if pos.bought_size > 0
                    else 0.0
                ),
                cost_usdc=pos.bought_cost_usdc,
                net_size=pos.net_size,
                realized_pnl_usdc=pos.realized_pnl_usdc,
                total_pnl_usdc=pnl,
                market_resolved=market_resolved,
                last_ts=pos.last_ts,
            )
        )

    for (_cond, outcome_idx), pos in positions.items():
        market = markets_by_condition.get(pos.condition_id)
        if market is None:
            total_pnl += pos.realized_pnl_usdc
            continue

        winner = _market_winning_outcome(market)
        if winner is None:
            # Market hasn't resolved yet, or was canceled — exclude from win/loss.
            total_pnl += pos.realized_pnl_usdc
            status = "exited" if pos.net_size <= 1e-9 else "open"
            _record(pos, outcome_idx, status, pos.realized_pnl_usdc,
                    market_resolved=False)
            continue

        if pos.net_size <= 1e-9:
            # Fully exited before resolution — count only realized PnL, no win/loss.
            total_pnl += pos.realized_pnl_usdc
            _record(pos, outcome_idx, "exited", pos.realized_pnl_usdc,
                    market_resolved=True)
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
                ts=pos.last_ts,
                category=_normalize_category(market.category),
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
        _record(pos, outcome_idx, "won" if won else "lost",
                pos.realized_pnl_usdc + unrealized, market_resolved=True)

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
        records=records,
    )


def _lead_bets_from_records(
    records: list[_PositionRecord],
    markets_by_condition: dict[str, PolymarketMarket],
) -> list[LeadBet]:
    """Reduce position records to the inputs price-lead scoring needs."""
    out: list[LeadBet] = []
    for rec in records:
        market = markets_by_condition.get(rec.condition_id)
        won: bool | None
        if rec.status == "won":
            won = True
        elif rec.status == "lost":
            won = False
        else:
            won = None
        out.append(
            LeadBet(
                condition_id=rec.condition_id,
                outcome_index=rec.outcome_index,
                event_slug=rec.event_slug,
                entry_price=rec.entry_price,
                cost_usdc=rec.cost_usdc,
                entry_ts=rec.last_ts,
                won=won,
                market_end_ts=parse_iso_ts(market.end_date) if market else 0,
            )
        )
    return out


def _enrichment_from_rollup(
    rollup: _WalletRollup,
    fit: PosteriorFit,
    *,
    ess: float,
    recent_fit: PosteriorFit,
    recent_ess: float,
    clv_stats: tuple[float, float, float] = (0.0, 0.0, 0.0),
    hydration: PolymarketWalletHydration | None = None,
    style: TraderStyle | None = None,
    lead: PriceLead | None = None,
) -> PolymarketWalletEnrichment:
    style = style or unclassified_style("style not computed")
    lead = lead or unmeasured_price_lead("price lead not computed")
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
    if recent_ess < MIN_RECENT_INDEPENDENT_EVENTS:
        tailability_reasons.append(
            "fewer than 5 recent independent settled events"
        )
    elif recent_fit.edge_mean < 0.0:
        # Only meaningful when there is enough recent data — with a near-empty
        # recent sample the fit just echoes the population prior.
        tailability_reasons.append("recent forecast edge is negative")
    # Closing-line value is the cleanest, market-mechanism-independent measure
    # of edge — did the wallet consistently get a better price than where the
    # market settled? Require statistically-confident positive CLV over an
    # adequate sample, not just a good win/loss-implied forecast fit. (clv_stats
    # is (mean, lower_bound, sample_size).)
    if clv_stats[2] < MIN_CLV_SAMPLE:
        tailability_reasons.append(
            f"fewer than {int(MIN_CLV_SAMPLE)} closing-line observations"
        )
    elif clv_stats[1] <= 0.0:
        tailability_reasons.append("closing-line value not confidently positive")
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
        recent_skill_likelihood=round(recent_fit.posterior_skill, 6),
        recent_edge_mean=round(recent_fit.edge_mean, 6),
        recent_edge_lower_bound=round(recent_fit.edge_lower_bound, 6),
        recent_independent_events=round(recent_ess, 4),
        category_edges=_category_edges(rollup.bets, fit),
        price_lead_score=lead.price_lead_score,
        price_lead_drivers=list(lead.drivers),
        post_entry_drift_48h=lead.post_entry_drift_48h,
        post_entry_drift_samples=lead.post_entry_drift_samples,
        longshot_win_rate=lead.longshot_win_rate,
        longshot_implied_rate=lead.longshot_implied_rate,
        longshot_events=lead.longshot_events,
        late_entry_win_rate=lead.late_entry_win_rate,
        late_entry_implied_rate=lead.late_entry_implied_rate,
        late_entry_events=lead.late_entry_events,
        clv_mean=round(clv_stats[0], 6),
        clv_lower_bound=round(clv_stats[1], 6),
        clv_sample_size=round(clv_stats[2], 4),
        style_archetype=style.archetype,
        automation_score=style.automation_score,
        style_drivers=list(style.drivers),
        trades_per_active_day=style.trades_per_active_day,
        median_intertrade_gap_seconds=style.median_intertrade_gap_seconds,
        active_utc_hours=style.active_utc_hours,
        buy_size_uniformity=style.buy_size_uniformity,
        markets_per_active_day=style.markets_per_active_day,
        top_category=style.top_category,
        top_category_share=style.top_category_share,
        exited_position_share=style.exited_position_share,
        median_exit_hold_hours=style.median_exit_hold_hours,
        data_quality_status=data_status,
        data_quality_reasons=data_reasons,
        economic_qualified=economic_qualified,
        tailability_status=tailability_status,
        tailability_reasons=tailability_reasons,
    )


def _records_clv(
    records: list[_PositionRecord], history: _PriceHistory
) -> tuple[tuple[float, float, float], list[float | None]]:
    """
    Per-record CLV plus the wallet-level (mean, lower_bound, sample_size).

    CLV_i = reference price of the picked outcome − buys-only entry VWAP,
    where the reference is the closing line for resolved markets and the
    latest observed line otherwise. Records without a usable reference (or
    entry) get None and are excluded from the aggregate.
    """
    observations: list[tuple[float, str, float]] = []
    per_record: list[float | None] = []
    for rec in records:
        if rec.entry_price <= 0.0:
            per_record.append(None)
            continue
        ref = _closing_line(history, rec.condition_id, resolved=rec.market_resolved)
        if ref is None or ref <= 0.0:
            per_record.append(None)
            continue
        picked_ref = ref if rec.outcome_index == 0 else 1.0 - ref
        clv = picked_ref - rec.entry_price
        per_record.append(clv)
        observations.append((clv, rec.event_slug, rec.cost_usdc))
    return _weighted_clv_stats(observations), per_record


def _wallet_bets_from_rollup(
    rollup: _WalletRollup,
    per_record_clv: list[float | None],
    markets_by_condition: dict[str, PolymarketMarket],
) -> list[PolymarketWalletBet]:
    out: list[PolymarketWalletBet] = []
    for rec, clv in zip(rollup.records, per_record_clv):
        market = markets_by_condition.get(rec.condition_id)
        outcome_label = ""
        if market and 0 <= rec.outcome_index < len(market.outcomes):
            outcome_label = market.outcomes[rec.outcome_index]
        out.append(
            PolymarketWalletBet(
                proxy_wallet=rollup.wallet,
                condition_id=rec.condition_id,
                outcome_index=rec.outcome_index,
                outcome=outcome_label
                or ("Yes" if rec.outcome_index == 0 else "No"),
                event_slug=rec.event_slug,
                category=market.category if market else "",
                title=market.question if market else "",
                status=rec.status,
                entry_price=round(rec.entry_price, 6),
                cost_usdc=round(rec.cost_usdc, 2),
                net_size=round(rec.net_size, 4),
                realized_pnl_usdc=round(rec.realized_pnl_usdc, 2),
                total_pnl_usdc=round(rec.total_pnl_usdc, 2),
                clv=round(clv, 6) if clv is not None else None,
                last_trade_ts=rec.last_ts,
            )
        )
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def _categories_index(
    markets_by_condition: dict[str, PolymarketMarket],
) -> dict[str, str]:
    return {
        cond: market.category
        for cond, market in markets_by_condition.items()
        if market.category
    }


def compute_wallet_enrichment(
    wallet: str,
    *,
    activity: list[PolymarketActivity],
    markets_by_condition: dict[str, PolymarketMarket],
    name: str = "",
    pseudonym: str = "",
    population_prior: PopulationPrior | None = None,
    hydration: PolymarketWalletHydration | None = None,
    price_snapshots: list[PolymarketPriceSnapshot] | None = None,
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
    # Single-wallet callers have no population-wide reference timestamp, so
    # the wallet's own newest bet anchors the recency decay.
    now_ts = max((bet.ts for bet in rollup.bets), default=0)
    recent_bets = apply_recency_weights(rollup.bets, now_ts=now_ts)
    recent_fit = fit_wallet_posterior(
        recent_bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2
    )
    recent_ess = effective_sample_size(recent_bets)
    history = build_price_history(
        list(markets_by_condition.values()), price_snapshots
    )
    clv_stats, _ = _records_clv(rollup.records, history)
    style = compute_trader_style(
        activity, categories_by_condition=_categories_index(markets_by_condition)
    )
    lead = compute_price_lead(
        _lead_bets_from_records(rollup.records, markets_by_condition), history
    )
    return _enrichment_from_rollup(
        rollup,
        fit,
        ess=ess,
        recent_fit=recent_fit,
        recent_ess=recent_ess,
        clv_stats=clv_stats,
        hydration=hydration,
        style=style,
        lead=lead,
    )


def compute_enrichment_outputs(
    *,
    activity: list[PolymarketActivity],
    markets: list[PolymarketMarket],
    leaderboard: list[PolymarketLeaderboardEntry],
    hydration_by_wallet: dict[str, PolymarketWalletHydration] | None = None,
    price_snapshots: list[PolymarketPriceSnapshot] | None = None,
) -> tuple[list[PolymarketWalletEnrichment], list[PolymarketWalletBet]]:
    """Single-shot wrapper around the shard-streaming core — use it when the
    activity list already fits in memory (tests, small datasets)."""
    return compute_enrichment_outputs_streaming(
        activity_shards_factory=lambda: iter([activity]),
        markets=markets,
        leaderboard=leaderboard,
        hydration_by_wallet=hydration_by_wallet,
        price_snapshots=price_snapshots,
    )


def compute_enrichment_outputs_streaming(
    *,
    activity_shards_factory: Callable[[], Iterator[list[PolymarketActivity]]],
    markets: list[PolymarketMarket],
    leaderboard: list[PolymarketLeaderboardEntry],
    hydration_by_wallet: dict[str, PolymarketWalletHydration] | None = None,
    price_snapshots: list[PolymarketPriceSnapshot] | None = None,
    bet_sink: Callable[[list[PolymarketWalletBet]], None] | None = None,
) -> tuple[list[PolymarketWalletEnrichment], list[PolymarketWalletBet]]:
    """
    Compute enrichment for every wallet present in the activity stream,
    plus the per-position betting records behind those aggregates.

    `activity_shards_factory` returns a fresh iterator over disjoint
    wallet-partitioned slices of the activity stream: every event for a given
    wallet must arrive in the same shard, in file order. It is called twice
    (see the two passes below), so it must re-yield the same shards each time
    — the on-disk shard reader does this by re-reading the files.

    Raw events are reduced to per-wallet aggregates one shard at a time, and
    crucially the rollups are NOT retained between passes: for the full wallet
    universe (thousands of whales, each with a heavy bet/record list) holding
    every rollup at once exceeds RAM. Instead pass 1 keeps only the lightweight
    per-wallet fit needed for the population prior, and pass 2 re-derives each
    rollup from the shards.

    `bet_sink`, when provided, receives each wallet's bet records as they are
    produced in pass 2 and the returned bet list is left empty — a full
    catalog resolves millions of records, and accumulating them (~6 GB) was
    the last unbounded sink after the rollup fix. Sink order is deterministic
    (shard order, then first-appearance order within a shard) but NOT sorted
    by wallet the way the accumulate-and-return path sorts.

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

    categories_by_condition = _categories_index(markets_by_condition)

    # Pass 1: reduce each shard's events to per-wallet aggregates, keeping only
    # the lightweight inputs later passes need — the weak-prior fit that feeds
    # the population prior, the trader style, and the recency reference. The
    # rollups themselves are discarded; pass 2 rebuilds them from the shards.
    weak_fits: list[PosteriorFit] = []
    styles_by_wallet: dict[str, TraderStyle] = {}
    now_ts = 0
    for shard in activity_shards_factory():
        by_wallet: dict[str, list[PolymarketActivity]] = defaultdict(list)
        for event in shard:
            by_wallet[event.proxy_wallet.lower()].append(event)
        for wallet, events in by_wallet.items():
            name, pseudonym = names_by_wallet.get(wallet, ("", ""))
            rollup = _roll_up_wallet(
                wallet, events, markets_by_condition,
                name=name, pseudonym=pseudonym,
            )
            fit = weak_prior_fit(rollup.bets)
            if fit is not None:
                weak_fits.append(fit)
            for bet in rollup.bets:
                if bet.ts > now_ts:
                    now_ts = bet.ts
            styles_by_wallet[wallet] = compute_trader_style(
                events, categories_by_condition=categories_by_condition,
            )

    # Empirical-Bayes population prior from the per-wallet MLEs (order-free).
    prior = population_prior_from_fits(weak_fits)
    history = build_price_history(markets, price_snapshots)

    # Pass 2: re-stream the shards, rebuild each rollup, and fit it under the
    # empirical prior (the shrinkage step). Re-deriving each rollup is cheaper
    # than having retained all of them across pass 1.
    out: list[PolymarketWalletEnrichment] = []
    all_bets: list[PolymarketWalletBet] = []
    for shard in activity_shards_factory():
        by_wallet = defaultdict(list)
        for event in shard:
            by_wallet[event.proxy_wallet.lower()].append(event)
        for wallet, events in by_wallet.items():
            name, pseudonym = names_by_wallet.get(wallet, ("", ""))
            rollup = _roll_up_wallet(
                wallet, events, markets_by_condition,
                name=name, pseudonym=pseudonym,
            )
            fit = fit_wallet_posterior(
                rollup.bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2
            )
            ess = effective_sample_size(rollup.bets)
            recent_bets = apply_recency_weights(rollup.bets, now_ts=now_ts)
            recent_fit = fit_wallet_posterior(
                recent_bets, mu_prior=prior.mu, sigma2_prior=prior.sigma2
            )
            recent_ess = effective_sample_size(recent_bets)
            clv_stats, per_record_clv = _records_clv(rollup.records, history)
            hydration = (hydration_by_wallet or {}).get(rollup.wallet)
            style = styles_by_wallet.get(rollup.wallet)
            lead = compute_price_lead(
                _lead_bets_from_records(rollup.records, markets_by_condition),
                history,
            )
            out.append(
                _enrichment_from_rollup(
                    rollup,
                    fit,
                    ess=ess,
                    recent_fit=recent_fit,
                    recent_ess=recent_ess,
                    clv_stats=clv_stats,
                    hydration=hydration,
                    style=style,
                    lead=lead,
                )
            )
            wallet_bets = _wallet_bets_from_rollup(
                rollup, per_record_clv, markets_by_condition
            )
            if bet_sink is not None:
                bet_sink(wallet_bets)
            else:
                all_bets.extend(wallet_bets)

    # Deterministic output regardless of shard order: enrichments by rank, and
    # bet records grouped by wallet (a stable sort preserves each wallet's own
    # record order, matching the single-shot path). Sink mode already handed
    # the records off, so there is nothing to sort.
    all_bets.sort(key=lambda b: b.proxy_wallet)
    out.sort(
        key=lambda e: (-e.rank_score, -e.skill_likelihood, e.proxy_wallet)
    )
    log.info(
        "enrichment_computed wallets=%d markets=%d resolved_bets=%d "
        "bet_records=%d prior=(mu=%.3f, sigma2=%.3f)",
        len(out),
        len(markets_by_condition),
        sum(e.resolved_trades for e in out),
        len(all_bets),
        prior.mu,
        prior.sigma2,
    )
    return out, all_bets


def compute_all_enrichment(
    *,
    activity: list[PolymarketActivity],
    markets: list[PolymarketMarket],
    leaderboard: list[PolymarketLeaderboardEntry],
    hydration_by_wallet: dict[str, PolymarketWalletHydration] | None = None,
    price_snapshots: list[PolymarketPriceSnapshot] | None = None,
) -> list[PolymarketWalletEnrichment]:
    """Back-compat wrapper around compute_enrichment_outputs (enrichment only)."""
    enrichments, _ = compute_enrichment_outputs(
        activity=activity,
        markets=markets,
        leaderboard=leaderboard,
        hydration_by_wallet=hydration_by_wallet,
        price_snapshots=price_snapshots,
    )
    return enrichments
