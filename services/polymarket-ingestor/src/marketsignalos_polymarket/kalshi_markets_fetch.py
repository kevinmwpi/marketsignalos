"""
Minimal Kalshi public markets fetcher used by the cross-exchange matcher.

The Kalshi /markets endpoint is unauthenticated, so we don't reuse the
keypair-aware client from services/ingestor — we just hit the public API
with httpx directly and persist the results to kalshi_markets.jsonl.

Paginates via Kalshi's cursor scheme. Stores one record per market with
the fields the matcher needs (ticker, title, category, expiration_time,
event_ticker, status).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any

import httpx

log = logging.getLogger("marketsignalos.polymarket.kalshi_fetch")

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class KalshiMarket:
    """Subset of fields needed by the matcher; raw_blob preserves the rest."""

    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    yes_sub_title: str
    category: str
    status: str
    expiration_time: str  # ISO-8601
    close_time: str
    fetched_at: str = field(default_factory=_utcnow_iso)


def _parse_market_row(row: dict[str, Any]) -> KalshiMarket | None:
    ticker = str(row.get("ticker", ""))
    if not ticker:
        return None
    return KalshiMarket(
        ticker=ticker,
        event_ticker=str(row.get("event_ticker", "")),
        title=str(row.get("title", "")),
        subtitle=str(row.get("subtitle", "")),
        yes_sub_title=str(row.get("yes_sub_title", "")),
        category=str(row.get("category", "") or ""),
        status=str(row.get("status", "")),
        expiration_time=str(row.get("expiration_time", "")),
        close_time=str(row.get("close_time", "")),
    )


def fetch_kalshi_markets(
    *,
    status: str = "settled",
    limit_per_page: int = 200,
    max_pages: int = 25,
    base_url: str | None = None,
    timeout_seconds: float = 15.0,
) -> list[KalshiMarket]:
    """
    Page through the public Kalshi /markets endpoint.
    status options: open | closed | settled (settled = resolved).
    """
    base = base_url or os.getenv("KALSHI_BASE_URL", DEFAULT_BASE_URL)
    headers = {"User-Agent": "MarketSignalOS-matcher/0.1", "Accept": "application/json"}

    all_markets: list[KalshiMarket] = []
    cursor: str | None = None

    with httpx.Client(timeout=timeout_seconds, headers=headers) as client:
        for page in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": limit_per_page}
            if cursor:
                params["cursor"] = cursor
            for attempt in range(3):
                resp = client.get(f"{base}/markets", params=params)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    sleep(0.5 * (2**attempt))
                    continue
                resp.raise_for_status()
                break
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Unexpected Kalshi /markets payload: {type(payload).__name__}")
            rows = payload.get("markets") or []
            for row in rows:
                if isinstance(row, dict):
                    parsed = _parse_market_row(row)
                    if parsed:
                        all_markets.append(parsed)
            cursor = payload.get("cursor") or None
            log.info("kalshi_markets page=%d status=%s fetched=%d cursor=%s", page, status, len(rows), bool(cursor))
            if not cursor or not rows:
                break
    return all_markets


def write_kalshi_markets_jsonl(markets: list[KalshiMarket], path: Path) -> int:
    """Truncate + rewrite. Markets list is small enough that we don't need dedupe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for m in markets:
            fh.write(json.dumps(asdict(m), separators=(",", ":")) + "\n")
    return len(markets)


def load_kalshi_markets_jsonl(path: Path) -> list[KalshiMarket]:
    if not path.exists():
        return []
    out: list[KalshiMarket] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            out.append(KalshiMarket(**obj))
        except (json.JSONDecodeError, TypeError):
            continue
    return out
