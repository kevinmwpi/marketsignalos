"""
Per-bet tradability classification for the skilled-bets feed.

Maps on-chain position signals to execution paths the operator can act on:
  poly_direct    — open/active Gamma market with a valid Polymarket deep link
  kalshi_mirror  — approved Polymarket→Kalshi match (fallback venue)
  closed         — Gamma marks the market closed or inactive
  on_chain_only  — still held on-chain but no verified tail path
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TradabilityStatus = Literal["poly_direct", "kalshi_mirror", "on_chain_only", "closed"]

_INTERNATIONAL_WEATHER_HINTS = (
    "uk ",
    " u.k.",
    "britain",
    "europe",
    "european",
    "germany",
    "france",
    "japan",
    "australia",
    "canada",
    "mexico",
    "brazil",
    "india",
    "china",
    "rainfall",
    "celsius",
    "fahrenheit",
)


@dataclass(frozen=True, slots=True)
class MarketTradabilityInput:
    active: bool
    closed: bool
    category: str


@dataclass(frozen=True, slots=True)
class KalshiMirrorInput:
    status: str
    confidence: float


def resolve_event_slug(*, position_slug: str, buy_slug: str, gamma_slug: str) -> str:
    """Prefer activity BUY slug, then position snapshot, then market slug."""
    for candidate in (buy_slug, position_slug, gamma_slug):
        cleaned = candidate.strip()
        if cleaned:
            return cleaned
    return ""


def classify_tradability(
    *,
    market: MarketTradabilityInput | None,
    event_slug: str,
    title: str,
    mirror: KalshiMirrorInput | None,
) -> tuple[TradabilityStatus, list[str]]:
    reasons: list[str] = []
    approved_kalshi = mirror is not None and mirror.status == "approved"
    pending_kalshi = mirror is not None and mirror.status == "pending"
    poly_url_viable = bool(event_slug.strip())

    if market is None:
        reasons.append("market missing from Gamma catalog")
    elif market.closed:
        reasons.append("market closed in Gamma catalog")
    elif not market.active:
        reasons.append("market inactive in Gamma catalog")

    if not poly_url_viable:
        reasons.append("no Polymarket event slug for deep link")

    poly_tradable = (
        market is not None
        and market.active
        and not market.closed
        and poly_url_viable
    )

    if poly_tradable:
        if pending_kalshi and mirror is not None:
            reasons.append(
                f"unreviewed Kalshi mirror pending ({mirror.confidence:.0%} confidence)"
            )
        return "poly_direct", reasons

    if approved_kalshi and mirror is not None:
        if market is not None and (market.closed or not market.active):
            reasons.append("Polymarket market unavailable; use approved Kalshi mirror")
        reasons.append(f"Kalshi match confidence {mirror.confidence:.0%}")
        return "kalshi_mirror", reasons

    if pending_kalshi:
        reasons.append("Kalshi mirror exists but is not approved for tailing")

    lowered = f"{title} {market.category if market else ''}".lower()
    if (
        (market is not None and market.category.lower() == "weather")
        or any(token in lowered for token in _INTERNATIONAL_WEATHER_HINTS)
    ) and not poly_tradable:
        reasons.append(
            "international or weather market may be on-chain only and absent from your catalog"
        )

    if market is not None and (market.closed or not market.active):
        return "closed", reasons

    return "on_chain_only", reasons


def is_actionable(status: TradabilityStatus | str) -> bool:
    return status in ("poly_direct", "kalshi_mirror")
