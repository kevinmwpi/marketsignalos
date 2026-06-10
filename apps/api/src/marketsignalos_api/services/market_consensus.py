"""
Smart-money confluence: group still-held skilled-wallet positions by market.

Independent skilled wallets agreeing on the same side of one market is much
stronger evidence than any single wallet's posterior — and skilled wallets on
OPPOSITE sides is a warning the market is genuinely contested. This service
is a group-by over the rows `compute_skilled_bets` already materializes (and
memoizes), so it adds no new file scans.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from marketsignalos_api.services.skilled_bets import SkilledBetSignal, compute_skilled_bets


@dataclass(slots=True)
class ConsensusSide:
    """All skilled wallets holding one (condition, outcome) side."""

    outcome_index: int
    outcome: str
    wallets: int = 0
    wallet_names: list[str] = field(default_factory=list)
    total_position_value_usdc: float = 0.0
    avg_entry_price: float = 0.0       # capital-weighted by entry USDC
    avg_skill_likelihood: float = 0.0  # mean across wallets on this side
    latest_entry_at: int = 0


@dataclass(slots=True)
class MarketConsensus:
    condition_id: str
    title: str
    category: str
    event_slug: str
    polymarket_market_url: str
    kalshi_market_url: str  # approved mirror only; "" otherwise
    tradability: str
    net_direction: str  # outcome label when one side is held; "contested" otherwise
    total_wallets: int
    sides: list[ConsensusSide] = field(default_factory=list)


def _display_name(signal: SkilledBetSignal) -> str:
    if signal.wallet_name:
        return signal.wallet_name
    wallet = signal.proxy_wallet
    return f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) >= 10 else wallet


def compute_market_consensus(
    *,
    min_skill: float = 0.8,
    min_resolved: int = 20,
    min_independent_events: float = 20.0,
    require_positive_edge: bool = False,
    min_wallets: int = 2,
    limit: int | None = 50,
) -> list[MarketConsensus]:
    """
    Markets aggregated across every still-held skilled position (including
    currently untradable ones — consensus is information even when the
    execution path is missing). Sorted by wallet count, then capital at risk.
    """
    signals = compute_skilled_bets(
        min_skill=min_skill,
        min_resolved=min_resolved,
        min_independent_events=min_independent_events,
        require_positive_edge=require_positive_edge,
        include_untradable=True,
        limit=None,
    )

    by_side: dict[tuple[str, int], list[SkilledBetSignal]] = {}
    for signal in signals:
        by_side.setdefault(
            (signal.condition_id, signal.outcome_index), []
        ).append(signal)

    by_cond: dict[str, list[ConsensusSide]] = {}
    meta_by_cond: dict[str, SkilledBetSignal] = {}
    for (cond, outcome_idx), rows in by_side.items():
        # One row per wallet per side (the feed already collapses to the
        # latest BUY per (wallet, condition, outcome)).
        seen: dict[str, SkilledBetSignal] = {}
        for row in rows:
            seen.setdefault(row.proxy_wallet, row)
        wallets = list(seen.values())

        total_value = sum(w.current_position_value_usdc for w in wallets)
        entry_capital = sum(max(0.0, w.entry_usdc_size) for w in wallets)
        avg_entry = (
            sum(w.entry_price * max(0.0, w.entry_usdc_size) for w in wallets)
            / entry_capital
            if entry_capital > 0
            else sum(w.entry_price for w in wallets) / len(wallets)
        )
        side = ConsensusSide(
            outcome_index=outcome_idx,
            outcome=wallets[0].outcome or ("Yes" if outcome_idx == 0 else "No"),
            wallets=len(wallets),
            wallet_names=sorted(_display_name(w) for w in wallets),
            total_position_value_usdc=round(total_value, 2),
            avg_entry_price=round(avg_entry, 4),
            avg_skill_likelihood=round(
                sum(w.skill_likelihood for w in wallets) / len(wallets), 6
            ),
            latest_entry_at=max(w.bought_at for w in wallets),
        )
        by_cond.setdefault(cond, []).append(side)
        existing = meta_by_cond.get(cond)
        if existing is None or wallets[0].bought_at > existing.bought_at:
            meta_by_cond[cond] = wallets[0]

    out: list[MarketConsensus] = []
    for cond, sides in by_cond.items():
        sides.sort(key=lambda s: (-s.wallets, -s.total_position_value_usdc))
        meta = meta_by_cond[cond]
        total_wallets = sum(s.wallets for s in sides)
        if total_wallets < min_wallets:
            continue
        out.append(
            MarketConsensus(
                condition_id=cond,
                title=meta.title,
                category=meta.category,
                event_slug=meta.event_slug,
                polymarket_market_url=meta.polymarket_market_url,
                kalshi_market_url=meta.kalshi_market_url,
                tradability=meta.tradability,
                net_direction=(
                    "contested" if len(sides) > 1 else sides[0].outcome
                ),
                total_wallets=total_wallets,
                sides=sides,
            )
        )

    out.sort(
        key=lambda m: (
            -m.total_wallets,
            -sum(s.total_position_value_usdc for s in m.sides),
            m.condition_id,
        )
    )
    if limit is None:
        return out
    return out[:limit]
