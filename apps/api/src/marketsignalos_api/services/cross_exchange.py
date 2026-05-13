"""
Cross-exchange dislocation signal.

Joins four data sources to surface tradeable opportunities:

  1. polymarket_wallet_enrichment.jsonl — skill scores per wallet
  2. polymarket_positions.jsonl         — wallets' currently-open positions
  3. market_links.jsonl                 — Kalshi <-> Polymarket identifier pairs
  4. kalshi_markets.jsonl               — Kalshi current YES prices
  5. polymarket_markets.jsonl           — Polymarket current YES prices

For each (skilled wallet, position-on-linked-market) tuple, compute the
price difference between the two exchanges. The signal ranks by
opportunity_score = dislocation_pct * skill_likelihood * log(1 + position_size_usdc).

Conventions:
  - Probability is stored on [0, 1] internally. Kalshi cents (0-100) are
    divided by 100 before comparison.
  - "YES" is outcome_index=0 by convention on Polymarket; Kalshi quotes
    the YES side directly. For a NO position, we flip both prices to the
    NO complement (1 - yes_price).
  - Dislocation sign convention: kalshi_yes - polymarket_yes. Positive
    means Kalshi YES is more expensive => buy YES on Polymarket / sell on
    Kalshi; negative means the opposite.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import (
    kalshi_markets_path,
    market_links_path,
    polymarket_enrichment_path,
    polymarket_markets_path,
    polymarket_positions_path,
)
from marketsignalos_api.services.external_urls import (
    kalshi_market_url,
    polymarket_market_url,
    polymarket_profile_url,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# ── Internal record shapes ────────────────────────────────────────────────────

@dataclass(slots=True)
class _WalletSkill:
    proxy_wallet: str
    name: str
    skill_likelihood: float
    resolved_trades: int
    total_pnl_usdc: float


@dataclass(slots=True)
class _Position:
    proxy_wallet: str
    condition_id: str
    outcome_index: int  # 0=YES, 1=NO
    outcome: str
    size: float
    avg_price: float
    current_value_usdc: float
    slug: str
    title: str
    snapshot_at: str

    @property
    def current_yes_price(self) -> float:
        """Implied current price for the YES outcome (probability)."""
        if self.size <= 0:
            return self.avg_price
        # current_value_usdc = size * current_price_of_held_outcome
        held = self.current_value_usdc / self.size
        # If position is on NO (outcome_index=1), invert to get YES price.
        return 1.0 - held if self.outcome_index == 1 else held


@dataclass(slots=True)
class _Link:
    kalshi_ticker: str
    polymarket_condition_id: str
    polymarket_slug: str
    kalshi_title: str
    polymarket_title: str
    confidence: float
    status: str


@dataclass(slots=True)
class _PolymarketPrice:
    condition_id: str
    yes_price: float


@dataclass(slots=True)
class _KalshiPrice:
    ticker: str
    yes_price: float  # 0..1
    title: str


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CrossExchangeSignal:
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float
    resolved_trades: int

    polymarket_condition_id: str
    polymarket_slug: str
    polymarket_title: str
    polymarket_outcome_index: int
    polymarket_outcome: str
    polymarket_yes_price: float
    polymarket_position_size: float
    polymarket_position_value_usdc: float

    kalshi_ticker: str
    kalshi_title: str
    kalshi_yes_price: float

    price_dislocation: float  # signed: kalshi_yes - polymarket_yes
    price_dislocation_pct: float  # absolute, * 100
    cheaper_exchange: str  # "kalshi" | "polymarket"
    recommended_action: str

    match_confidence: float
    match_status: str

    opportunity_score: float
    observed_at: str

    # Deep links for "tail the whale" — open the wallet, the Polymarket leg,
    # and the corresponding Kalshi market in one click each. Empty string when
    # the identifying field upstream was blank.
    polymarket_profile_url: str
    polymarket_market_url: str
    kalshi_market_url: str


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_skills() -> dict[str, _WalletSkill]:
    out: dict[str, _WalletSkill] = {}
    for row in _read_jsonl(polymarket_enrichment_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        out[wallet] = _WalletSkill(
            proxy_wallet=wallet,
            name=str(row.get("name", "") or ""),
            skill_likelihood=_f(row.get("skill_likelihood")),
            resolved_trades=int(row.get("resolved_trades", 0) or 0),
            total_pnl_usdc=_f(row.get("total_pnl_usdc")),
        )
    return out


def _load_latest_positions() -> list[_Position]:
    """
    Positions are appended as snapshots — collapse to the most recent
    snapshot per (wallet, condition_id, outcome_index).
    """
    latest: dict[tuple[str, str, int], _Position] = {}
    for row in _read_jsonl(polymarket_positions_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        cond = str(row.get("condition_id", ""))
        if not wallet or not cond:
            continue
        try:
            outcome_index = int(row.get("outcome_index", 0) or 0)
        except (TypeError, ValueError):
            continue
        snap_at = str(row.get("snapshot_at", "") or "")
        pos = _Position(
            proxy_wallet=wallet,
            condition_id=cond,
            outcome_index=outcome_index,
            outcome=str(row.get("outcome", "")),
            size=_f(row.get("size")),
            avg_price=_f(row.get("avg_price")),
            current_value_usdc=_f(row.get("current_value_usdc")),
            slug=str(row.get("slug", "")),
            title=str(row.get("title", "")),
            snapshot_at=snap_at,
        )
        key = (wallet, cond, outcome_index)
        prior = latest.get(key)
        if prior is None or pos.snapshot_at > prior.snapshot_at:
            latest[key] = pos
    return [p for p in latest.values() if p.size > 0]


def _load_links_by_condition() -> dict[str, list[_Link]]:
    """One condition_id may map to multiple Kalshi tickers — keep them all."""
    out: dict[str, list[_Link]] = {}
    for row in _read_jsonl(market_links_path()):
        status = str(row.get("status", ""))
        if status == "rejected":
            continue
        cond = str(row.get("polymarket_condition_id", ""))
        if not cond:
            continue
        link = _Link(
            kalshi_ticker=str(row.get("kalshi_ticker", "")),
            polymarket_condition_id=cond,
            polymarket_slug=str(row.get("polymarket_slug", "")),
            kalshi_title=str(row.get("kalshi_title", "")),
            polymarket_title=str(row.get("polymarket_title", "")),
            confidence=_f(row.get("confidence")),
            status=status,
        )
        out.setdefault(cond, []).append(link)
    return out


def _load_polymarket_prices() -> dict[str, _PolymarketPrice]:
    """
    Index by condition_id. Prefer last_trade_price; fall back to mid of bid/ask.
    """
    out: dict[str, _PolymarketPrice] = {}
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
            continue
        if price <= 0 or price >= 1:
            continue
        out[cond] = _PolymarketPrice(condition_id=cond, yes_price=price)
    return out


def _load_kalshi_prices() -> dict[str, _KalshiPrice]:
    """
    Index by ticker. Kalshi cents (0..100) -> decimal probability.
    Prefer mid of yes_bid/yes_ask; fall back to last_price.
    """
    out: dict[str, _KalshiPrice] = {}
    for row in _read_jsonl(kalshi_markets_path()):
        ticker = str(row.get("ticker", ""))
        if not ticker:
            continue
        yes_bid = _f(row.get("yes_bid"))
        yes_ask = _f(row.get("yes_ask"))
        last = _f(row.get("last_price"))
        cents: float
        if yes_bid > 0 and yes_ask > 0:
            cents = (yes_bid + yes_ask) / 2.0
        elif last > 0:
            cents = last
        else:
            continue
        price = cents / 100.0
        if price <= 0 or price >= 1:
            continue
        out[ticker] = _KalshiPrice(ticker=ticker, yes_price=price, title=str(row.get("title", "")))
    return out


# ── Signal assembly ───────────────────────────────────────────────────────────

def compute_cross_exchange_signals(
    *,
    min_skill: float = 0.6,
    min_resolved: int = 5,
    min_dislocation_pct: float = 2.0,
    min_position_value_usdc: float = 0.0,
    only_approved_links: bool = False,
) -> list[CrossExchangeSignal]:
    skills = _load_skills()
    positions = _load_latest_positions()
    links_by_cond = _load_links_by_condition()
    poly_prices = _load_polymarket_prices()
    kalshi_prices = _load_kalshi_prices()

    observed_at = _utcnow_iso()
    signals: list[CrossExchangeSignal] = []

    for pos in positions:
        skill = skills.get(pos.proxy_wallet)
        if skill is None or skill.skill_likelihood < min_skill or skill.resolved_trades < min_resolved:
            continue
        if pos.current_value_usdc < min_position_value_usdc:
            continue

        candidate_links = links_by_cond.get(pos.condition_id)
        if not candidate_links:
            continue

        # Prefer Polymarket market's live price; fall back to position's implied price.
        poly_price_rec = poly_prices.get(pos.condition_id)
        polymarket_yes_price = (
            poly_price_rec.yes_price if poly_price_rec else pos.current_yes_price
        )

        for link in candidate_links:
            if only_approved_links and link.status != "approved":
                continue
            kalshi_rec = kalshi_prices.get(link.kalshi_ticker)
            if kalshi_rec is None:
                continue

            dislocation = kalshi_rec.yes_price - polymarket_yes_price
            dislocation_pct = abs(dislocation) * 100.0
            if dislocation_pct < min_dislocation_pct:
                continue

            cheaper = "polymarket" if dislocation > 0 else "kalshi"
            if pos.outcome_index == 0:
                action = (
                    f"Buy YES on {cheaper} (skilled wallet long YES @ "
                    f"${polymarket_yes_price:.3f}; other side @ ${kalshi_rec.yes_price:.3f})"
                )
            else:
                # Wallet holds NO; opportunity exists where NO is cheaper.
                action = (
                    f"Buy NO on {'kalshi' if cheaper == 'kalshi' else 'polymarket'} "
                    f"(skilled wallet long NO; YES prices: Poly=${polymarket_yes_price:.3f}, "
                    f"Kalshi=${kalshi_rec.yes_price:.3f})"
                )

            opp_score = dislocation_pct * skill.skill_likelihood * math.log1p(
                max(0.0, pos.current_value_usdc)
            )

            resolved_slug = pos.slug or link.polymarket_slug
            signals.append(
                CrossExchangeSignal(
                    proxy_wallet=pos.proxy_wallet,
                    wallet_name=skill.name,
                    skill_likelihood=round(skill.skill_likelihood, 6),
                    resolved_trades=skill.resolved_trades,
                    polymarket_condition_id=pos.condition_id,
                    polymarket_slug=resolved_slug,
                    polymarket_title=pos.title or link.polymarket_title,
                    polymarket_outcome_index=pos.outcome_index,
                    polymarket_outcome=pos.outcome,
                    polymarket_yes_price=round(polymarket_yes_price, 4),
                    polymarket_position_size=round(pos.size, 4),
                    polymarket_position_value_usdc=round(pos.current_value_usdc, 2),
                    kalshi_ticker=link.kalshi_ticker,
                    kalshi_title=kalshi_rec.title or link.kalshi_title,
                    kalshi_yes_price=round(kalshi_rec.yes_price, 4),
                    price_dislocation=round(dislocation, 4),
                    price_dislocation_pct=round(dislocation_pct, 4),
                    cheaper_exchange=cheaper,
                    recommended_action=action,
                    match_confidence=round(link.confidence, 4),
                    match_status=link.status,
                    opportunity_score=round(opp_score, 4),
                    observed_at=observed_at,
                    polymarket_profile_url=polymarket_profile_url(pos.proxy_wallet),
                    polymarket_market_url=polymarket_market_url(resolved_slug),
                    kalshi_market_url=kalshi_market_url(link.kalshi_ticker),
                )
            )

    signals.sort(key=lambda s: -s.opportunity_score)
    return signals
