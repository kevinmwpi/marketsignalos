"""
"Recent active bets from skilled wallets" feed.

Joins three JSONL stores:
  1. polymarket_wallet_enrichment.jsonl — to identify skilled wallets
  2. polymarket_positions.jsonl         — to filter to STILL-HELD positions
  3. polymarket_activity.jsonl          — to surface the most-recent BUY
                                          that established or added to each
                                          still-held position

Definition of "still held": the latest snapshot of (wallet, condition_id,
outcome_index) has size > 0. Positions that have been fully exited
(size=0) or never appeared on a snapshot are excluded — those bets are
no longer tailable.

Definition of "entry": the most recent TRADE/BUY event matching the same
(wallet, condition_id, outcome_index). If the wallet entered, sold some,
then bought more, the latest BUY is the most informative "they just
added to this position" signal. Wallets that bought once long ago and
have held since show that earlier timestamp — both behaviors are useful.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import (
    kalshi_markets_path,
    market_links_path,
    polymarket_activity_path,
    polymarket_enrichment_path,
    polymarket_markets_path,
    polymarket_positions_path,
)
from marketsignalos_api.services.external_urls import (
    kalshi_market_url,
    polymarket_market_url,
    polymarket_profile_url,
)


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SkilledBetSignal:
    # Wallet provenance
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float
    resolved_trades: int
    win_rate: float

    # Market
    condition_id: str
    slug: str
    event_slug: str
    title: str
    category: str

    # The bet — latest BUY entry for this still-held position
    outcome_index: int
    outcome: str
    entry_price: float
    entry_size: float
    entry_usdc_size: float
    transaction_hash: str
    bought_at: int  # unix seconds

    # What the wallet still has on the table
    current_position_size: float
    current_position_value_usdc: float
    current_market_yes_price: float  # 0 if unknown

    # Deep links
    polymarket_profile_url: str
    polymarket_market_url: str

    # Kalshi mirror — populated when the cross-exchange matcher found an
    # equivalent market on Kalshi. Empty strings / 0 / None when no match
    # exists, so the client can use truthiness to decide whether to render
    # the "tail this on Kalshi" call-to-action.
    kalshi_ticker: str
    kalshi_event_ticker: str
    kalshi_title: str
    kalshi_market_url: str
    kalshi_yes_price: float  # 0.0..1.0; 0.0 when unknown
    kalshi_match_confidence: float  # 0.0..1.0
    kalshi_match_status: str  # "approved" | "pending" | ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _f(val: Any) -> float:
    try:
        if val is None or val == "":
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _i(val: Any) -> int:
    try:
        if val is None or val == "":
            return 0
        return int(float(val))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class _SkilledWallet:
    proxy_wallet: str
    name: str
    skill_likelihood: float
    resolved_trades: int
    win_rate: float


@dataclass(slots=True)
class _HeldPosition:
    proxy_wallet: str
    condition_id: str
    outcome_index: int
    outcome: str
    slug: str
    event_slug: str
    title: str
    size: float
    current_value_usdc: float
    snapshot_at: str


@dataclass(slots=True)
class _PolymarketMarketRec:
    condition_id: str
    category: str
    yes_price: float  # last_trade_price or mid; 0 if unknown


def _load_skilled_wallets(
    *, min_skill: float, min_resolved: int
) -> dict[str, _SkilledWallet]:
    out: dict[str, _SkilledWallet] = {}
    for row in _read_jsonl(polymarket_enrichment_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        skill = _f(row.get("skill_likelihood"))
        resolved = _i(row.get("resolved_trades"))
        if skill < min_skill or resolved < min_resolved:
            continue
        out[wallet] = _SkilledWallet(
            proxy_wallet=wallet,
            name=str(row.get("name", "") or ""),
            skill_likelihood=skill,
            resolved_trades=resolved,
            win_rate=_f(row.get("win_rate")),
        )
    return out


def _load_held_positions(
    *, skilled_wallets: set[str]
) -> dict[tuple[str, str, int], _HeldPosition]:
    """
    Collapse the snapshot append-only file to the latest snapshot per
    (wallet, condition, outcome). Drop anything with size <= 0 (no longer
    held) or where the wallet isn't in the skilled set.
    """
    latest: dict[tuple[str, str, int], _HeldPosition] = {}
    for row in _read_jsonl(polymarket_positions_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if wallet not in skilled_wallets:
            continue
        cond = str(row.get("condition_id", ""))
        if not cond:
            continue
        outcome_idx = _i(row.get("outcome_index"))
        snap_at = str(row.get("snapshot_at", "") or "")
        pos = _HeldPosition(
            proxy_wallet=wallet,
            condition_id=cond,
            outcome_index=outcome_idx,
            outcome=str(row.get("outcome", "")),
            slug=str(row.get("slug", "")),
            event_slug=str(row.get("event_slug", "") or ""),
            title=str(row.get("title", "")),
            size=_f(row.get("size")),
            current_value_usdc=_f(row.get("current_value_usdc")),
            snapshot_at=snap_at,
        )
        key = (wallet, cond, outcome_idx)
        prior = latest.get(key)
        if prior is None or pos.snapshot_at > prior.snapshot_at:
            latest[key] = pos
    # Filter to "still held" — non-trivial size in the most recent snapshot.
    return {k: p for k, p in latest.items() if p.size > 1e-9}


def _load_polymarket_markets() -> dict[str, _PolymarketMarketRec]:
    out: dict[str, _PolymarketMarketRec] = {}
    for row in _read_jsonl(polymarket_markets_path()):
        cond = str(row.get("condition_id", ""))
        if not cond:
            continue
        last = row.get("last_trade_price")
        bid = row.get("best_bid")
        ask = row.get("best_ask")
        if last is not None:
            price = _f(last)
        elif bid is not None and ask is not None:
            price = (_f(bid) + _f(ask)) / 2.0
        else:
            price = 0.0
        out[cond] = _PolymarketMarketRec(
            condition_id=cond,
            category=str(row.get("category", "") or ""),
            yes_price=price,
        )
    return out


@dataclass(slots=True)
class _KalshiMirror:
    """Best Kalshi mirror for one Polymarket condition_id."""

    ticker: str
    event_ticker: str
    title: str
    yes_price: float  # 0..1, normalized from Kalshi cents
    confidence: float
    status: str  # "approved" | "pending"


def _load_kalshi_price_index() -> dict[str, tuple[str, float, str]]:
    """ticker -> (event_ticker, yes_price_decimal, title). yes_price is 0
    when neither bid/ask mid nor last trade is available."""
    out: dict[str, tuple[str, float, str]] = {}
    for row in _read_jsonl(kalshi_markets_path()):
        ticker = str(row.get("ticker", ""))
        if not ticker:
            continue
        yes_bid = _f(row.get("yes_bid"))
        yes_ask = _f(row.get("yes_ask"))
        last = _f(row.get("last_price"))
        cents: float = 0.0
        if yes_bid > 0 and yes_ask > 0:
            cents = (yes_bid + yes_ask) / 2.0
        elif last > 0:
            cents = last
        price = cents / 100.0 if cents > 0 else 0.0
        out[ticker] = (
            str(row.get("event_ticker", "") or ""),
            price,
            str(row.get("title", "") or ""),
        )
    return out


def _load_best_kalshi_mirror_by_cond() -> dict[str, _KalshiMirror]:
    """For each Polymarket condition_id, pick the highest-confidence
    non-rejected link. Manual approvals always beat auto matches at the
    same confidence (matched_by == 'manual' tiebreak)."""
    kalshi_index = _load_kalshi_price_index()
    best: dict[str, _KalshiMirror] = {}
    for row in _read_jsonl(market_links_path()):
        status = str(row.get("status", ""))
        if status == "rejected":
            continue
        cond = str(row.get("polymarket_condition_id", ""))
        ticker = str(row.get("kalshi_ticker", ""))
        if not cond or not ticker:
            continue
        confidence = _f(row.get("confidence"))
        manual = str(row.get("matched_by", "")) == "manual"

        event_ticker, yes_price, kalshi_title_live = kalshi_index.get(
            ticker, ("", 0.0, "")
        )
        # Prefer the live title from kalshi_markets.jsonl over the snapshot
        # in market_links — the former is what the user will see when they
        # follow the link.
        kalshi_title = kalshi_title_live or str(row.get("kalshi_title", ""))

        candidate = _KalshiMirror(
            ticker=ticker,
            event_ticker=event_ticker,
            title=kalshi_title,
            yes_price=yes_price,
            confidence=confidence,
            status=status,
        )
        existing = best.get(cond)
        if existing is None:
            best[cond] = candidate
            continue
        # Strict preference: approved > pending; then confidence; then manual.
        def _rank(m: _KalshiMirror, is_manual: bool) -> tuple[int, float, int]:
            return (1 if m.status == "approved" else 0, m.confidence, 1 if is_manual else 0)

        if _rank(candidate, manual) > _rank(existing, False):
            best[cond] = candidate
    return best


def _latest_buy_per_position(
    *,
    keys: set[tuple[str, str, int]],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """
    Single pass over the activity file: keep the highest-timestamp BUY event
    per (wallet, condition, outcome) key. We only care about keys that have
    a still-held position — anything else is dead weight.
    """
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in _read_jsonl(polymarket_activity_path()):
        if row.get("type") != "TRADE":
            continue
        if str(row.get("side", "")).upper() != "BUY":
            continue
        wallet = str(row.get("proxy_wallet", "")).lower()
        cond = str(row.get("condition_id", ""))
        outcome_idx = _i(row.get("outcome_index"))
        key = (wallet, cond, outcome_idx)
        if key not in keys:
            continue
        prior = out.get(key)
        if prior is None or _i(row.get("timestamp")) > _i(prior.get("timestamp")):
            out[key] = row
    return out


# ── Public entry point ────────────────────────────────────────────────────────

def compute_skilled_bets(
    *,
    min_skill: float = 0.8,
    min_resolved: int = 20,
    min_position_value_usdc: float = 0.0,
) -> list[SkilledBetSignal]:
    """
    Returns recent BUY entries from skilled wallets that are STILL HELD,
    sorted by entry timestamp (newest first).
    """
    skilled = _load_skilled_wallets(
        min_skill=min_skill, min_resolved=min_resolved
    )
    if not skilled:
        return []

    held = _load_held_positions(skilled_wallets=set(skilled.keys()))
    if not held:
        return []

    markets = _load_polymarket_markets()
    latest_buys = _latest_buy_per_position(keys=set(held.keys()))
    kalshi_mirrors = _load_best_kalshi_mirror_by_cond()

    out: list[SkilledBetSignal] = []
    for key, pos in held.items():
        if pos.current_value_usdc < min_position_value_usdc:
            continue
        wallet = skilled[pos.proxy_wallet]
        buy = latest_buys.get(key)
        if buy is None:
            # Wallet still holds the position but we never observed the entry
            # in the activity window. Skip — we can't say when they bought.
            continue
        market_rec = markets.get(pos.condition_id)
        mirror = kalshi_mirrors.get(pos.condition_id)
        out.append(
            SkilledBetSignal(
                proxy_wallet=pos.proxy_wallet,
                wallet_name=wallet.name,
                skill_likelihood=round(wallet.skill_likelihood, 6),
                resolved_trades=wallet.resolved_trades,
                win_rate=round(wallet.win_rate, 4),
                condition_id=pos.condition_id,
                slug=pos.slug,
                event_slug=pos.event_slug,
                title=pos.title or str(buy.get("title", "")),
                category=market_rec.category if market_rec else "",
                outcome_index=pos.outcome_index,
                outcome=pos.outcome,
                entry_price=round(_f(buy.get("price")), 4),
                entry_size=round(_f(buy.get("size")), 4),
                entry_usdc_size=round(_f(buy.get("usdc_size")), 2),
                transaction_hash=str(buy.get("transaction_hash", "")),
                bought_at=_i(buy.get("timestamp")),
                current_position_size=round(pos.size, 4),
                current_position_value_usdc=round(pos.current_value_usdc, 2),
                current_market_yes_price=round(
                    market_rec.yes_price if market_rec else 0.0, 4
                ),
                polymarket_profile_url=polymarket_profile_url(pos.proxy_wallet),
                polymarket_market_url=polymarket_market_url(pos.event_slug),
                kalshi_ticker=mirror.ticker if mirror else "",
                kalshi_event_ticker=mirror.event_ticker if mirror else "",
                kalshi_title=mirror.title if mirror else "",
                kalshi_market_url=(
                    kalshi_market_url(mirror.event_ticker) if mirror else ""
                ),
                kalshi_yes_price=round(mirror.yes_price, 4) if mirror else 0.0,
                kalshi_match_confidence=(
                    round(mirror.confidence, 4) if mirror else 0.0
                ),
                kalshi_match_status=mirror.status if mirror else "",
            )
        )

    out.sort(key=lambda s: -s.bought_at)
    return out
