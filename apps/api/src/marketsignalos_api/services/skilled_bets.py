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
import logging
import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import (
    kalshi_markets_path,
    market_links_path,
    polymarket_activity_path,
    polymarket_enrichment_path,
    polymarket_markets_path,
    polymarket_position_snapshots_path,
    polymarket_positions_path,
    polymarket_wallet_values_path,
)

from marketsignalos_api.services.external_urls import (
    kalshi_market_url,
    polymarket_market_url,
    polymarket_profile_url,
)
from marketsignalos_api.services.tradability import (
    KalshiMirrorInput,
    MarketTradabilityInput,
    classify_tradability,
    is_actionable,
    resolve_event_slug,
)

log = logging.getLogger("marketsignalos.api.skilled_bets")


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SkilledBetSignal:
    # Wallet provenance
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float        # P(edge > 0 | data)
    resolved_trades: int
    win_rate: float
    edge_mean: float               # posterior edge in log-odds
    edge_lower_bound: float        # 5th-percentile lower bound (conservative)
    forecast_skill_likelihood: float
    forecast_edge_mean: float
    forecast_edge_lower_bound: float
    independent_settled_events: float
    all_time_pnl_usdc: float
    all_time_volume_usdc: float
    all_time_roi: float
    pnl_30d_usdc: float
    active_pnl_usdc: float
    max_drawdown_usdc: float
    data_quality_status: str
    data_quality_reasons: list[str]
    economic_qualified: bool
    tailability_status: str
    tailability_reasons: list[str]
    score_version: str

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
    current_outcome_price: float

    # Deep links
    polymarket_profile_url: str
    polymarket_market_url: str

    # Kalshi mirror — populated when the Polymarket→Kalshi market matcher
    # found an equivalent market on Kalshi. Empty strings / 0 / None when
    # no match exists, so the client can use truthiness to decide whether
    # to render the "tail this on Kalshi" call-to-action.
    kalshi_ticker: str
    kalshi_event_ticker: str
    kalshi_title: str
    kalshi_market_url: str
    kalshi_yes_price: float  # 0.0..1.0; 0.0 when unknown
    kalshi_match_confidence: float  # 0.0..1.0
    kalshi_match_status: str  # "approved" | "pending" | ""
    kalshi_vs_entry_cents: float  # Kalshi YES minus wallet entry; 0 when unknown

    # Execution path for this specific bet (may differ from wallet tailability).
    tradability: str  # poly_direct | kalshi_mirror | on_chain_only | closed
    tradability_reasons: list[str]
    signal_age_days: int

    # Remaining-edge assessment: how much of the wallet's thesis the market
    # has already priced in since their entry.
    #   move_captured_pct     (current - entry) / (1 - entry), clamped to [-1, 1].
    #                         0 = price still at entry; 1 = fully converged to $1;
    #                         negative = price now BELOW the wallet's entry.
    #   remaining_edge_status "discounted" | "fresh" | "partial" | "late" | "unknown"
    move_captured_pct: float
    remaining_edge_status: str

    # Smart-money confluence on this market.
    #   consensus_wallets    distinct skilled wallets holding this same
    #                        (condition, outcome) side — including this one.
    #   consensus_contested  True when skilled wallets hold BOTH sides of
    #                        this market.
    consensus_wallets: int = 1
    consensus_contested: bool = False

    # Position-sizing conviction: skilled traders size up when conviction is
    # high. conviction_z is this entry's USDC size z-scored against the
    # wallet's OWN historical BUY sizes; the label requires >= 5 prior buys.
    #   conviction              "high" (z>=2) | "low" (z<=-1) | "normal" | "unknown"
    #   entry_pct_of_bankroll   entry USDC / latest wallet portfolio value; 0 unknown
    conviction: str = "unknown"
    conviction_z: float = 0.0
    entry_pct_of_bankroll: float = 0.0

    # Wallet-level closing-line value (from enrichment; see ingestor docs).
    clv_mean: float = 0.0
    clv_lower_bound: float = 0.0
    clv_sample_size: float = 0.0

    # Wallet trading style (from enrichment; see ingestor trader_style.py).
    # Descriptive only — "systematic" wallets are automation-shaped operations,
    # "discretionary" are human-paced. Never gates inclusion in the feed.
    wallet_archetype: str = "unclassified"
    wallet_automation_score: float = 0.0

    # Price-lead footprint (from enrichment; see ingestor price_lead.py):
    # 0..1 score of whether the market tends to follow this wallet's entries
    # (post-entry drift, longshot conversion, late-entry accuracy).
    # Descriptive only — never gates inclusion.
    wallet_price_lead_score: float = 0.0

    # Tail expected value at TODAY's price (see the tail-EV section above).
    #   tail_edge_used   conservative log-odds edge applied (lifetime lower
    #                    bound, capped by the recent-form bound when available
    #                    and by the entry-category bound when that category
    #                    has enough independent events)
    #   tail_fair_price  sigmoid(logit(entry) + tail_edge_used); 0 when the
    #                    entry price is unusable
    #   tail_ev          tail_fair_price − current price, $/share; 0 unknown
    #   tail_ev_status   "positive" | "marginal" | "negative" | "unknown"
    tail_edge_used: float = 0.0
    tail_fair_price: float = 0.0
    tail_ev: float = 0.0
    tail_ev_status: str = "unknown"

    # Per-category skill for THIS entry's category (from the enrichment
    # category_edges decomposition). When the category has fewer than 5
    # independent settled events — or the market has no category — the
    # wallet-level score is echoed and the source says so.
    #   category_skill_source  "category" | "wallet_fallback"
    category_skill_likelihood: float = 0.0
    category_edge_lower_bound: float = 0.0
    category_independent_events: float = 0.0
    category_skill_source: str = "wallet_fallback"


# ── Remaining-edge classification ─────────────────────────────────────────────
#
# A tailing user cares whether the wallet's thesis is still buyable near the
# wallet's own entry. move_captured_pct measures the fraction of the maximum
# possible move (entry → $1) the market has already made.

_REMAINING_EDGE_SORT_RANK = {
    "discounted": 0,  # price below entry — better than the wallet got
    "fresh": 0,       # ≤25% of the move captured
    "partial": 1,     # 25–75% captured
    "unknown": 1,     # no live price — don't promote, don't bury
    "late": 2,        # >75% captured — mostly priced in
}


def _remaining_edge(entry_price: float, current_price: float) -> tuple[float, str]:
    """(move_captured_pct, status) for a bet entered at entry_price whose
    picked outcome now trades at current_price. Prices are 0..1 probabilities."""
    if current_price <= 0.0 or entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0, "unknown"
    captured = (current_price - entry_price) / (1.0 - entry_price)
    captured = max(-1.0, min(1.0, captured))
    if captured < -0.02:
        status = "discounted"
    elif captured <= 0.25:
        status = "fresh"
    elif captured <= 0.75:
        status = "partial"
    else:
        status = "late"
    return round(captured, 4), status


# ── Tail expected value ───────────────────────────────────────────────────────
#
# The Bayesian skill model (ingestor's bayesian_skill.py) estimates a per-wallet
# log-odds edge: the wallet's picked outcomes win at rate
# sigmoid(logit(entry_price) + edge). Applying the CONSERVATIVE edge bound to
# the wallet's entry-implied probability gives an explainable fair-price
# estimate for the picked outcome; subtracting the live price gives the
# expected value — in probability units, i.e. dollars per share — of tailing
# at TODAY's price rather than the wallet's. This is the number that answers
# "does buying now still beat the odds the market is quoting?".

# EV inside this buffer is within typical spread + fees — call it marginal
# rather than positive so the feed doesn't oversell paper-thin edges.
_TAIL_EV_MARGINAL_BUFFER = 0.02
# Mirrors the ingestor's MIN_RECENT_INDEPENDENT_EVENTS: below this the recency
# fit mostly echoes the population prior, so it can't tighten the bound.
_MIN_RECENT_EVENTS_FOR_TAIL_EDGE = 5.0
# Mirrors the ingestor's CATEGORY_MIN_INDEPENDENT_EVENTS: below this a
# category fit is dominated by its prior (the wallet's lifetime posterior)
# and adds nothing over the wallet-level score.
_MIN_CATEGORY_EVENTS_FOR_TAIL_EDGE = 5.0
_PRICE_EPS = 1e-3


def _safe_logit(p: float) -> float:
    clamped = min(max(p, _PRICE_EPS), 1.0 - _PRICE_EPS)
    return math.log(clamped / (1.0 - clamped))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _tail_edge_for(wallet: _SkilledWallet) -> float:
    """Conservative log-odds edge for EV: the lifetime 5th-percentile bound,
    further capped by the recency-weighted bound when the wallet has enough
    recent independent events for that fit to mean anything."""
    edge = wallet.edge_lower_bound
    if wallet.recent_independent_events >= _MIN_RECENT_EVENTS_FOR_TAIL_EDGE:
        edge = min(edge, wallet.recent_edge_lower_bound)
    return edge


def _tail_ev(
    entry_price: float, current_price: float, tail_edge: float
) -> tuple[float, float, str]:
    """(fair_price, ev, status) of tailing this bet at current_price.

    fair_price = sigmoid(logit(entry_price) + tail_edge) — the win probability
    the wallet's conservative edge implies for the picked outcome. ev is
    fair_price − current_price. Status is "unknown" when either price is
    unobserved, else "positive" / "marginal" (inside the spread buffer) /
    "negative".
    """
    if entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0, 0.0, "unknown"
    fair = _sigmoid(_safe_logit(entry_price) + tail_edge)
    if current_price <= 0.0 or current_price >= 1.0:
        return round(fair, 4), 0.0, "unknown"
    ev = fair - current_price
    if ev >= _TAIL_EV_MARGINAL_BUFFER:
        status = "positive"
    elif ev > 0.0:
        status = "marginal"
    else:
        status = "negative"
    return round(fair, 4), round(ev, 4), status


# ── Internal helpers ──────────────────────────────────────────────────────────

def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream one dict per JSONL line. Streaming (rather than read_text +
    splitlines) keeps peak memory flat: polymarket_activity.jsonl can exceed
    1GB / millions of rows, and slurping it whole spikes to several GB and can
    OOM. Callers that only need a subset should filter as they consume."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Materialize all rows. Fine for the small stores (positions, markets,
    kalshi, enrichment, links); do NOT use for the activity file — iterate
    _iter_jsonl and filter inline instead."""
    return list(_iter_jsonl(path))


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
    edge_mean: float
    edge_lower_bound: float
    independent_settled_events: float
    all_time_pnl_usdc: float
    all_time_volume_usdc: float
    all_time_roi: float
    pnl_30d_usdc: float
    active_pnl_usdc: float
    max_drawdown_usdc: float
    data_quality_status: str
    data_quality_reasons: list[str]
    economic_qualified: bool
    tailability_status: str
    tailability_reasons: list[str]
    score_version: str
    clv_mean: float = 0.0
    clv_lower_bound: float = 0.0
    clv_sample_size: float = 0.0
    style_archetype: str = "unclassified"
    automation_score: float = 0.0
    recent_edge_lower_bound: float = 0.0
    recent_independent_events: float = 0.0
    price_lead_score: float = 0.0
    # normalized category -> (skill_likelihood, edge_lower_bound, independent_events)
    category_edges: dict[str, tuple[float, float, float]] = field(default_factory=dict)


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
    current_outcome_price: float
    snapshot_id: str
    snapshot_at: str


@dataclass(slots=True)
class _PolymarketMarketRec:
    condition_id: str
    category: str
    slug: str
    event_slug: str
    active: bool
    closed: bool
    yes_price: float  # last_trade_price or mid; 0 if unknown
    fetched_at: str


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
        category_edges: dict[str, tuple[float, float, float]] = {}
        for edge_row in row.get("category_edges") or []:
            if not isinstance(edge_row, dict):
                continue
            category_key = str(edge_row.get("category", "") or "").strip().lower()
            if not category_key:
                continue
            category_edges[category_key] = (
                _f(edge_row.get("skill_likelihood")),
                _f(edge_row.get("edge_lower_bound")),
                _f(edge_row.get("independent_events")),
            )
        if (
            # Older rows (no recency fit / no category edges) stay eligible so
            # the feed survives a deploy until the next enrichment pass
            # rewrites the file at the current version.
            str(row.get("score_version", ""))
            not in ("forecast-v2", "forecast-v3", "forecast-v4")
            or str(row.get("data_quality_status", "untrusted")) != "trusted"
            or str(row.get("tailability_status", "blocked")) != "tailable"
            or skill < min_skill
            or resolved < min_resolved
        ):
            continue
        out[wallet] = _SkilledWallet(
            proxy_wallet=wallet,
            name=str(row.get("name", "") or ""),
            skill_likelihood=skill,
            resolved_trades=resolved,
            win_rate=_f(row.get("win_rate")),
            # forecast_* duplicates edge_* in current enrichment rows; the
            # fallback covers older rows that only carry the forecast_ names.
            edge_mean=_f(row.get("edge_mean", row.get("forecast_edge_mean"))),
            edge_lower_bound=_f(
                row.get("edge_lower_bound", row.get("forecast_edge_lower_bound"))
            ),
            independent_settled_events=_f(row.get("independent_settled_events")),
            all_time_pnl_usdc=_f(row.get("all_time_pnl_usdc")),
            all_time_volume_usdc=_f(row.get("all_time_volume_usdc")),
            all_time_roi=_f(row.get("all_time_roi")),
            pnl_30d_usdc=_f(row.get("pnl_30d_usdc")),
            active_pnl_usdc=_f(row.get("active_pnl_usdc")),
            max_drawdown_usdc=_f(row.get("max_drawdown_usdc")),
            data_quality_status=str(row.get("data_quality_status", "untrusted")),
            data_quality_reasons=[
                str(reason) for reason in (row.get("data_quality_reasons") or [])
            ],
            economic_qualified=bool(row.get("economic_qualified", False)),
            tailability_status=str(row.get("tailability_status", "blocked")),
            tailability_reasons=[
                str(reason) for reason in (row.get("tailability_reasons") or [])
            ],
            score_version=str(row.get("score_version", "legacy")),
            clv_mean=_f(row.get("clv_mean")),
            clv_lower_bound=_f(row.get("clv_lower_bound")),
            clv_sample_size=_f(row.get("clv_sample_size")),
            style_archetype=str(row.get("style_archetype", "") or "unclassified"),
            automation_score=_f(row.get("automation_score")),
            recent_edge_lower_bound=_f(row.get("recent_edge_lower_bound")),
            recent_independent_events=_f(row.get("recent_independent_events")),
            price_lead_score=_f(row.get("price_lead_score")),
            category_edges=category_edges,
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
    manifests = {
        str(row.get("proxy_wallet", "")).lower(): str(row.get("snapshot_id", ""))
        for row in _read_jsonl(polymarket_position_snapshots_path())
        if row.get("complete") is True and row.get("snapshot_id")
    }
    latest: dict[tuple[str, str, int], _HeldPosition] = {}
    for row in _read_jsonl(polymarket_positions_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if wallet not in skilled_wallets:
            continue
        snapshot_id = str(row.get("snapshot_id", ""))
        if not snapshot_id or manifests.get(wallet) != snapshot_id:
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
            current_outcome_price=_f(row.get("current_outcome_price")),
            snapshot_id=snapshot_id,
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
            slug=str(row.get("slug", "") or ""),
            event_slug=str(row.get("event_slug", "") or ""),
            active=bool(row.get("active", False)),
            closed=bool(row.get("closed", False)),
            yes_price=price,
            fetched_at=str(row.get("fetched_at", "") or ""),
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


@dataclass(slots=True)
class _BuySizeStats:
    """Streaming accumulator for one wallet's historical BUY sizes (USDC)."""

    count: int = 0
    total: float = 0.0
    total_sq: float = 0.0

    def add(self, usdc: float) -> None:
        self.count += 1
        self.total += usdc
        self.total_sq += usdc * usdc

    def zscore(self, usdc: float) -> float | None:
        """None when the history is too thin to standardize against."""
        if self.count < _MIN_BUYS_FOR_CONVICTION:
            return None
        mean = self.total / self.count
        variance = max(0.0, self.total_sq / self.count - mean * mean)
        std = math.sqrt(variance)
        if std < 1e-9:
            return None
        return (usdc - mean) / std


_MIN_BUYS_FOR_CONVICTION = 5
_CONVICTION_HIGH_Z = 2.0
_CONVICTION_LOW_Z = -1.0


def _conviction_label(z: float | None) -> str:
    if z is None:
        return "unknown"
    if z >= _CONVICTION_HIGH_Z:
        return "high"
    if z <= _CONVICTION_LOW_Z:
        return "low"
    return "normal"


def _latest_buy_per_position(
    *,
    keys: set[tuple[str, str, int]],
    stat_wallets: set[str],
) -> tuple[dict[tuple[str, str, int], dict[str, Any]], dict[str, _BuySizeStats]]:
    """
    Single pass over the activity file: keep the highest-timestamp BUY event
    per (wallet, condition, outcome) key, and accumulate each skilled
    wallet's full BUY-size history (for conviction z-scores) in the same
    stream so the >1GB file is still only read once.
    """
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    stats: dict[str, _BuySizeStats] = {}
    # Stream + filter inline: the activity file can be >1GB, so we must never
    # materialize all rows. We retain only the (small) set of rows whose key
    # matches a still-held position — at most one row per key.
    for row in _iter_jsonl(polymarket_activity_path()):
        if row.get("type") != "TRADE":
            continue
        if str(row.get("side", "")).upper() != "BUY":
            continue
        wallet = str(row.get("proxy_wallet", "")).lower()
        cond = str(row.get("condition_id", ""))
        outcome_idx = _i(row.get("outcome_index"))
        if wallet in stat_wallets:
            stats.setdefault(wallet, _BuySizeStats()).add(_f(row.get("usdc_size")))
        key = (wallet, cond, outcome_idx)
        if key not in keys:
            continue
        prior = out.get(key)
        if prior is None or _i(row.get("timestamp")) > _i(prior.get("timestamp")):
            out[key] = row
    return out, stats


def _load_wallet_values() -> dict[str, float]:
    """Latest portfolio value per wallet (the values file is append-only)."""
    out: dict[str, tuple[str, float]] = {}
    for row in _read_jsonl(polymarket_wallet_values_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        snapshot_at = str(row.get("snapshot_at", "") or "")
        prior = out.get(wallet)
        if prior is None or snapshot_at >= prior[0]:
            out[wallet] = (snapshot_at, _f(row.get("value_usdc")))
    return {wallet: value for wallet, (_, value) in out.items()}


# ── Caching ─────────────────────────────────────────────────────────────────
#
# compute_skilled_bets reads the full polymarket_activity.jsonl (can be >1GB
# for high-volume wallets) and json-parses every row, which takes tens of
# seconds. The dashboard renders server-side with force-dynamic, so without a
# cache every page load pays that cost twice. We memoize on a fingerprint of
# the input files (path, mtime_ns, size) plus the query params: any ingest —
# via the API button or the CLI — changes a file's mtime/size and so produces
# a fresh key, auto-invalidating stale results. invalidate_cache() is also
# called explicitly when an API-triggered ingest finishes, so the very next
# request recomputes even on filesystems with coarse mtime resolution.

# Inputs compute_skilled_bets depends on. Order is irrelevant to correctness;
# the fingerprint covers all of them.
def _input_paths() -> tuple[Path, ...]:
    return (
        polymarket_enrichment_path(),
        polymarket_positions_path(),
        polymarket_position_snapshots_path(),
        polymarket_markets_path(),
        polymarket_activity_path(),
        kalshi_markets_path(),
        market_links_path(),
        polymarket_wallet_values_path(),
    )


def _inputs_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """(path, mtime_ns, size) per input file; missing files contribute zeros.
    Cheap (a stat per file) relative to the multi-second recompute it guards."""
    parts: list[tuple[str, int, int]] = []
    for path in _input_paths():
        try:
            st = path.stat()
            parts.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(path), 0, 0))
    return tuple(parts)


@lru_cache(maxsize=32)
def _compute_skilled_bets_cached(
    _fingerprint: tuple[tuple[str, int, int], ...],
    min_skill: float,
    min_resolved: int,
    min_position_value_usdc: float,
    min_independent_events: float,
    max_bet_age_days: int,
    include_untradable: bool,
    require_positive_edge: bool,
    max_move_captured: float,
) -> tuple[SkilledBetSignal, ...]:
    # _fingerprint is unused inside the body — it exists only to key the cache
    # on input-file state so changed data forces a recompute.
    return tuple(
        _compute_skilled_bets(
            min_skill=min_skill,
            min_resolved=min_resolved,
            min_position_value_usdc=min_position_value_usdc,
            min_independent_events=min_independent_events,
            max_bet_age_days=max_bet_age_days,
            include_untradable=include_untradable,
            require_positive_edge=require_positive_edge,
            max_move_captured=max_move_captured,
        )
    )


def invalidate_cache() -> None:
    """Drop all memoized skilled-bets results. Called after an ingest run so
    the next request recomputes immediately rather than waiting for the file
    fingerprint to change."""
    _compute_skilled_bets_cached.cache_clear()
    log.info("skilled_bets cache invalidated")


def summarize_skilled_bets() -> dict[str, Any]:
    """Research-facing trust and tailability counts for rebuild diagnostics."""
    trusted = rebuilding = blocked = tailable = 0
    reasons: Counter[str] = Counter()
    for row in _read_jsonl(polymarket_enrichment_path()):
        if str(row.get("data_quality_status", "untrusted")) == "trusted":
            trusted += 1
        else:
            rebuilding += 1
        if str(row.get("tailability_status", "blocked")) == "tailable":
            tailable += 1
        else:
            blocked += 1
            raw_reasons = row.get("tailability_reasons") or ["legacy or incomplete score"]
            if isinstance(raw_reasons, list):
                reasons.update(str(reason) for reason in raw_reasons)

    all_signals = compute_skilled_bets(include_untradable=True, limit=None)
    tradability_counts: Counter[str] = Counter(signal.tradability for signal in all_signals)
    latest_snapshot = max(
        (signal.bought_at for signal in all_signals),
        default=0,
    )
    return {
        "trusted": trusted,
        "rebuilding": rebuilding,
        "blocked": blocked,
        "tailable": tailable,
        "blocked_reasons": dict(sorted(reasons.items())),
        "feed_poly_direct": tradability_counts.get("poly_direct", 0),
        "feed_kalshi_mirror": tradability_counts.get("kalshi_mirror", 0),
        "feed_on_chain_only": tradability_counts.get("on_chain_only", 0),
        "feed_closed": tradability_counts.get("closed", 0),
        "feed_actionable": sum(
            tradability_counts.get(key, 0) for key in ("poly_direct", "kalshi_mirror")
        ),
        "latest_signal_at": latest_snapshot,
    }


# ── Public entry point ────────────────────────────────────────────────────────

def compute_skilled_bets(
    *,
    min_skill: float = 0.8,
    min_resolved: int = 20,
    min_position_value_usdc: float = 0.0,
    min_independent_events: float = 20.0,
    max_bet_age_days: int = 0,
    include_untradable: bool = False,
    require_positive_edge: bool = False,
    max_move_captured: float = 1.0,
    limit: int | None = 50,
) -> list[SkilledBetSignal]:
    """
    Returns recent BUY entries from skilled wallets that are STILL HELD.

    Ordering: remaining-edge rank first (fresh/discounted entries before
    partially-priced ones, fully-converged "late" signals last), then
    high-conviction entries, then tail EV at today's price (best first), then
    entry timestamp (newest first). When `max_move_captured` < 1, signals
    whose move_captured_pct meets or exceeds it are dropped entirely.
    Memoized on input-file fingerprint + params; see the caching note above.
    """
    cached = _compute_skilled_bets_cached(
        _inputs_fingerprint(),
        min_skill,
        min_resolved,
        min_position_value_usdc,
        min_independent_events,
        max_bet_age_days,
        include_untradable,
        require_positive_edge,
        max_move_captured,
    )
    signals = list(cached)
    if limit is None:
        return signals
    return signals[:limit]


def _compute_skilled_bets(
    *,
    min_skill: float = 0.8,
    min_resolved: int = 20,
    min_position_value_usdc: float = 0.0,
    min_independent_events: float = 20.0,
    max_bet_age_days: int = 0,
    include_untradable: bool = False,
    require_positive_edge: bool = False,
    max_move_captured: float = 1.0,
) -> list[SkilledBetSignal]:
    """Uncached worker — reads the JSONL stores and builds the signal list."""
    skilled = _load_skilled_wallets(
        min_skill=min_skill, min_resolved=min_resolved
    )
    if not skilled:
        return []

    held = _load_held_positions(skilled_wallets=set(skilled.keys()))
    if not held:
        return []

    markets = _load_polymarket_markets()
    latest_buys, buy_stats = _latest_buy_per_position(
        keys=set(held.keys()), stat_wallets=set(skilled.keys())
    )
    kalshi_mirrors = _load_best_kalshi_mirror_by_cond()
    wallet_values = _load_wallet_values()
    now_ts = int(time.time())

    # Pass 1: build every candidate row that clears the WALLET-level gates and
    # has an observed BUY entry. Per-bet filters (value, age, move captured,
    # tradability) are deferred to pass 2 so consensus counts reflect every
    # skilled position on the market, not just the rows that survive filters.
    candidates: list[SkilledBetSignal] = []
    for key, pos in held.items():
        wallet = skilled[pos.proxy_wallet]
        if wallet.independent_settled_events < min_independent_events:
            continue
        if require_positive_edge and wallet.edge_lower_bound <= 0.0:
            continue
        buy = latest_buys.get(key)
        if buy is None:
            # Wallet still holds the position but we never observed the entry
            # in the activity window. Skip — we can't say when they bought.
            continue
        bought_at = _i(buy.get("timestamp"))
        signal_age_days = max(0, (now_ts - bought_at) // 86400) if bought_at else 0
        market_rec = markets.get(pos.condition_id)
        mirror = kalshi_mirrors.get(pos.condition_id)
        event_slug = resolve_event_slug(
            position_slug=pos.event_slug,
            buy_slug=str(buy.get("event_slug", "") or ""),
            gamma_slug=market_rec.event_slug if market_rec else "",
        )
        market_input = (
            MarketTradabilityInput(
                active=market_rec.active,
                closed=market_rec.closed,
                category=market_rec.category,
            )
            if market_rec
            else None
        )
        mirror_input = (
            KalshiMirrorInput(status=mirror.status, confidence=mirror.confidence)
            if mirror
            else None
        )
        title = pos.title or str(buy.get("title", ""))
        tradability, tradability_reasons = classify_tradability(
            market=market_input,
            event_slug=event_slug,
            title=title,
            mirror=mirror_input,
        )
        entry_price = round(_f(buy.get("price")), 4)
        # Live price of the PICKED outcome: prefer the position snapshot's
        # outcome price; fall back to the Gamma market YES price (inverted
        # for NO-side bets).
        current_outcome = pos.current_outcome_price
        if current_outcome <= 0.0 and market_rec and market_rec.yes_price > 0.0:
            current_outcome = (
                market_rec.yes_price
                if pos.outcome_index == 0
                else 1.0 - market_rec.yes_price
            )
        move_captured, remaining_edge_status = _remaining_edge(
            entry_price, current_outcome
        )
        # Per-category skill for this entry's own category. A proven category
        # fit caps the tail edge (a politics sharp's sports bets get priced
        # at their sports edge, not their overall one); a thin or missing
        # category falls back to the wallet-level score.
        category_raw = market_rec.category if market_rec else ""
        category_fit = wallet.category_edges.get(category_raw.strip().lower())
        tail_edge = _tail_edge_for(wallet)
        if (
            category_fit is not None
            and category_fit[2] >= _MIN_CATEGORY_EVENTS_FOR_TAIL_EDGE
        ):
            category_skill, category_lb, category_events = category_fit
            category_source = "category"
            tail_edge = min(tail_edge, category_lb)
        else:
            category_skill = wallet.skill_likelihood
            category_lb = wallet.edge_lower_bound
            category_events = category_fit[2] if category_fit is not None else 0.0
            category_source = "wallet_fallback"
        tail_fair_price, tail_ev, tail_ev_status = _tail_ev(
            entry_price, current_outcome, tail_edge
        )
        entry_usdc = _f(buy.get("usdc_size"))
        conviction_z = (
            stats.zscore(entry_usdc)
            if (stats := buy_stats.get(pos.proxy_wallet)) is not None
            else None
        )
        bankroll = wallet_values.get(pos.proxy_wallet, 0.0)
        entry_pct_of_bankroll = (
            round(entry_usdc / bankroll, 6) if bankroll > 0 and entry_usdc > 0 else 0.0
        )
        kalshi_yes = round(mirror.yes_price, 4) if mirror else 0.0
        kalshi_vs_entry_cents = (
            round((kalshi_yes - entry_price) * 100.0, 2)
            if kalshi_yes > 0 and entry_price > 0
            else 0.0
        )
        candidates.append(
            SkilledBetSignal(
                proxy_wallet=pos.proxy_wallet,
                wallet_name=wallet.name,
                skill_likelihood=round(wallet.skill_likelihood, 6),
                resolved_trades=wallet.resolved_trades,
                win_rate=round(wallet.win_rate, 4),
                edge_mean=round(wallet.edge_mean, 4),
                edge_lower_bound=round(wallet.edge_lower_bound, 4),
                forecast_skill_likelihood=round(wallet.skill_likelihood, 6),
                forecast_edge_mean=round(wallet.edge_mean, 4),
                forecast_edge_lower_bound=round(wallet.edge_lower_bound, 4),
                independent_settled_events=round(wallet.independent_settled_events, 4),
                all_time_pnl_usdc=round(wallet.all_time_pnl_usdc, 2),
                all_time_volume_usdc=round(wallet.all_time_volume_usdc, 2),
                all_time_roi=round(wallet.all_time_roi, 8),
                pnl_30d_usdc=round(wallet.pnl_30d_usdc, 2),
                active_pnl_usdc=round(wallet.active_pnl_usdc, 2),
                max_drawdown_usdc=round(wallet.max_drawdown_usdc, 2),
                data_quality_status=wallet.data_quality_status,
                data_quality_reasons=wallet.data_quality_reasons,
                economic_qualified=wallet.economic_qualified,
                tailability_status=wallet.tailability_status,
                tailability_reasons=wallet.tailability_reasons,
                score_version=wallet.score_version,
                condition_id=pos.condition_id,
                slug=pos.slug,
                event_slug=event_slug,
                title=title,
                category=market_rec.category if market_rec else "",
                outcome_index=pos.outcome_index,
                outcome=pos.outcome,
                entry_price=entry_price,
                entry_size=round(_f(buy.get("size")), 4),
                entry_usdc_size=round(_f(buy.get("usdc_size")), 2),
                transaction_hash=str(buy.get("transaction_hash", "")),
                bought_at=bought_at,
                signal_age_days=signal_age_days,
                current_position_size=round(pos.size, 4),
                current_position_value_usdc=round(pos.current_value_usdc, 2),
                current_market_yes_price=round(
                    market_rec.yes_price if market_rec else 0.0, 4
                ),
                current_outcome_price=round(pos.current_outcome_price, 4),
                polymarket_profile_url=polymarket_profile_url(pos.proxy_wallet),
                polymarket_market_url=polymarket_market_url(event_slug),
                kalshi_ticker=mirror.ticker if mirror else "",
                kalshi_event_ticker=mirror.event_ticker if mirror else "",
                kalshi_title=mirror.title if mirror else "",
                kalshi_market_url=(
                    kalshi_market_url(mirror.event_ticker)
                    if mirror and mirror.status == "approved"
                    else ""
                ),
                kalshi_yes_price=kalshi_yes,
                kalshi_match_confidence=(
                    round(mirror.confidence, 4) if mirror else 0.0
                ),
                kalshi_match_status=mirror.status if mirror else "",
                kalshi_vs_entry_cents=kalshi_vs_entry_cents,
                tradability=tradability,
                tradability_reasons=tradability_reasons,
                move_captured_pct=move_captured,
                remaining_edge_status=remaining_edge_status,
                conviction=_conviction_label(conviction_z),
                conviction_z=round(conviction_z, 4) if conviction_z is not None else 0.0,
                entry_pct_of_bankroll=entry_pct_of_bankroll,
                clv_mean=round(wallet.clv_mean, 6),
                clv_lower_bound=round(wallet.clv_lower_bound, 6),
                clv_sample_size=round(wallet.clv_sample_size, 4),
                wallet_archetype=wallet.style_archetype,
                wallet_automation_score=round(wallet.automation_score, 4),
                wallet_price_lead_score=round(wallet.price_lead_score, 4),
                tail_edge_used=round(tail_edge, 4),
                tail_fair_price=tail_fair_price,
                tail_ev=tail_ev,
                tail_ev_status=tail_ev_status,
                category_skill_likelihood=round(category_skill, 6),
                category_edge_lower_bound=round(category_lb, 4),
                category_independent_events=round(category_events, 4),
                category_skill_source=category_source,
            )
        )

    # Consensus maps over ALL candidates: distinct skilled wallets per
    # (condition, outcome) side, and which sides of each market are held.
    wallets_by_side: dict[tuple[str, int], set[str]] = defaultdict(set)
    for signal in candidates:
        wallets_by_side[(signal.condition_id, signal.outcome_index)].add(
            signal.proxy_wallet
        )
    sides_by_cond: dict[str, set[int]] = defaultdict(set)
    for cond, outcome_idx in wallets_by_side:
        sides_by_cond[cond].add(outcome_idx)

    # Pass 2: per-bet filters + consensus annotation.
    out: list[SkilledBetSignal] = []
    for signal in candidates:
        if signal.current_position_value_usdc < min_position_value_usdc:
            continue
        if max_bet_age_days > 0 and signal.signal_age_days > max_bet_age_days:
            continue
        if (
            max_move_captured < 1.0
            and signal.remaining_edge_status != "unknown"
            and signal.move_captured_pct >= max_move_captured
        ):
            continue
        if not include_untradable and not is_actionable(signal.tradability):
            continue
        signal.consensus_wallets = len(
            wallets_by_side[(signal.condition_id, signal.outcome_index)]
        )
        signal.consensus_contested = len(sides_by_cond[signal.condition_id]) > 1
        out.append(signal)

    # Down-rank converged signals: a stale entry is worse than no entry for a
    # tailing user. Within each remaining-edge rank, unusually-large
    # ("high conviction") entries first, then best tail EV at today's price
    # (unknown-EV rows sort as 0 — between positive and negative), then
    # newest BUY.
    out.sort(
        key=lambda s: (
            _REMAINING_EDGE_SORT_RANK.get(s.remaining_edge_status, 1),
            0 if s.conviction == "high" else 1,
            -s.tail_ev,
            -s.bought_at,
        )
    )
    return out
