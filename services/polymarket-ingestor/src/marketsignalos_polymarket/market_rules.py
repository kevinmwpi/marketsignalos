"""
Fetch one market's resolution rules text from Kalshi or Polymarket, by id.

Used to build frozen eval cases for the Polymarket -> Kalshi matcher: a human
labels a pair while reading the same titles, dates and rules text that a model
judge will later receive, and the case stores that text with its fetch time so
later rule edits on either exchange cannot silently change an old case.

Field provenance (see docs/llm-judge.md):
  - Polymarket Gamma ``/markets`` rows carry ``description`` (the resolution
    rules) and ``resolutionSource``. Observed in a real probe recorded in
    docs/polymarket-api-discovery.md.
  - Kalshi v2 ``GET /markets/{ticker}`` wraps the market in ``{"market": ...}``
    and carries ``rules_primary`` / ``rules_secondary``. Not yet observed from a
    live response in this repository.

Neither assumption is trusted silently. If a real payload lacks the expected
keys entirely, the fetch raises ``RulesFieldError`` naming the keys it did see,
so a wrong field name fails the first run instead of producing blank rules.
An empty value for a present key is legitimate and is kept as-is.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

KALSHI_RULE_FIELDS: tuple[str, ...] = ("rules_primary", "rules_secondary")
POLYMARKET_RULE_FIELDS: tuple[str, ...] = ("description", "resolutionSource")


class RulesFieldError(RuntimeError):
    """A real exchange payload did not have the shape this module expects."""


class MarketNotFound(LookupError):
    """The exchange positively reported that the market does not exist."""


# (url, query params) -> parsed JSON. Raises MarketNotFound on HTTP 404 and
# lets any other transport or HTTP failure propagate to the caller.
GetJson = Callable[[str, dict[str, str]], Any]


@dataclass(frozen=True, slots=True)
class FetchedMarket:
    exchange: str  # "kalshi" | "polymarket"
    market_id: str  # Kalshi ticker | Polymarket condition id
    title: str
    subtitle: str  # Kalshi bracket text (yes_sub_title); empty for Polymarket
    secondary_id: str  # Kalshi event_ticker | Polymarket slug
    end_date: str
    rules: str
    rules_fields: tuple[str, ...]  # payload keys the rules text was read from
    fetched_at: str
    payload_keys: tuple[str, ...]  # every key seen, for field verification


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def http_get_json(timeout_seconds: float = 15.0) -> GetJson:
    """Default network implementation of ``GetJson``."""
    headers = {"User-Agent": "MarketSignalOS-matcher-eval/0.1", "Accept": "application/json"}

    def get(url: str, params: dict[str, str]) -> Any:
        with httpx.Client(timeout=timeout_seconds, headers=headers) as client:
            response = client.get(url, params=params)
        if response.status_code == 404:
            raise MarketNotFound(url)
        response.raise_for_status()
        return response.json()

    return get


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def fetch_kalshi_market(
    ticker: str, *, get_json: GetJson, base_url: str | None = None,
) -> FetchedMarket | None:
    """Return the market with its rules text, or None if Kalshi reports 404."""
    base = (base_url or os.environ.get("KALSHI_BASE_URL") or KALSHI_BASE_URL).rstrip("/")
    try:
        payload = get_json(f"{base}/markets/{ticker}", {})
    except MarketNotFound:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("market"), dict):
        seen = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise RulesFieldError(f"Kalshi /markets/{ticker}: expected {{'market': {{...}}}}, saw {seen}")
    market: dict[str, Any] = payload["market"]
    present = tuple(name for name in KALSHI_RULE_FIELDS if name in market)
    if not present:
        raise RulesFieldError(
            f"Kalshi market {ticker} has none of {KALSHI_RULE_FIELDS}; keys seen: {sorted(market)}"
        )
    rules = "\n\n".join(text for name in present if (text := _text(market[name])))
    return FetchedMarket(
        exchange="kalshi",
        market_id=ticker,
        title=_text(market.get("title")),
        subtitle=_text(market.get("yes_sub_title")) or _text(market.get("subtitle")),
        secondary_id=_text(market.get("event_ticker")),
        # Same precedence as runner._kalshi_to_normalized, so the eval sees the
        # date the production matcher compared.
        end_date=_text(market.get("expiration_time")) or _text(market.get("close_time")),
        rules=rules,
        rules_fields=present,
        fetched_at=_utcnow_iso(),
        payload_keys=tuple(sorted(market)),
    )


def fetch_polymarket_market(
    condition_id: str, *, get_json: GetJson, base_url: str | None = None,
) -> FetchedMarket | None:
    """Return the market with its rules text, or None if Gamma returns no such row.

    Gamma filters on ``closed``, so an id is looked up under both filters
    (open first) before it is reported as not returned.
    """
    base = (base_url or os.environ.get("POLYMARKET_GAMMA_BASE_URL") or GAMMA_BASE_URL).rstrip("/")
    wanted = condition_id.lower()
    row: dict[str, Any] | None = None
    for closed in ("false", "true"):
        try:
            rows = get_json(
                f"{base}/markets", {"condition_ids": condition_id, "closed": closed, "limit": "5"},
            )
        except MarketNotFound:
            continue
        if not isinstance(rows, list):
            raise RulesFieldError(
                f"Gamma /markets?condition_ids={condition_id}: expected a list, "
                f"saw {type(rows).__name__}"
            )
        matches = [
            r for r in rows
            if isinstance(r, dict) and str(r.get("conditionId", "")).lower() == wanted
        ]
        if matches:
            row = matches[0]
            break
    if row is None:
        return None
    if "description" not in row:
        raise RulesFieldError(
            f"Gamma market {condition_id} has no 'description'; keys seen: {sorted(row)}"
        )
    description = _text(row.get("description"))
    source = _text(row.get("resolutionSource"))
    rules = description
    if source:
        rules = f"{description}\n\nResolution source: {source}" if description else source
    present = tuple(name for name in POLYMARKET_RULE_FIELDS if name in row)
    return FetchedMarket(
        exchange="polymarket",
        market_id=condition_id,
        title=_text(row.get("question")),
        subtitle="",
        secondary_id=_text(row.get("slug")),
        end_date=_text(row.get("endDate")),
        rules=rules,
        rules_fields=present,
        fetched_at=_utcnow_iso(),
        payload_keys=tuple(sorted(row)),
    )
