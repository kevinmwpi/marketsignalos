"""
Sync HTTP client for Polymarket's public APIs.

All endpoints are unauthenticated. We hit four hostnames:
  - lb-api.polymarket.com   (leaderboards)
  - data-api.polymarket.com (per-wallet activity, positions, value)
  - gamma-api.polymarket.com (market metadata)
  - api.goldsky.com         (subgraph for backfill — not yet wired)

Retry semantics mirror the Kalshi client: exponential backoff on 429/5xx,
respects Retry-After when present.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from time import sleep
from typing import Any, cast

import httpx

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_SUBGRAPH = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/polymarket-orderbook-resync/prod/gn"
)

log = logging.getLogger("marketsignalos.polymarket.client")


@dataclass(frozen=True, slots=True)
class PolymarketClientConfig:
    timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    user_agent: str = "MarketSignalOS-polymarket/0.1"

    @classmethod
    def from_env(cls) -> PolymarketClientConfig:
        return cls(
            timeout_seconds=float(os.getenv("POLYMARKET_TIMEOUT_SECONDS", "15.0")),
            max_retries=int(os.getenv("POLYMARKET_MAX_RETRIES", "3")),
        )


class PolymarketClient:
    """Sync client. Caller is responsible for calling close()."""

    def __init__(
        self,
        config: PolymarketClientConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config or PolymarketClientConfig()
        self._client = client or httpx.Client(
            timeout=self._config.timeout_seconds,
            headers={
                "User-Agent": self._config.user_agent,
                "Accept": "application/json",
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    # ── Leaderboard ───────────────────────────────────────────────────────────

    def get_leaderboard(
        self,
        *,
        metric: str = "profit",
        window: str = "all",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Returns raw leaderboard rows. metric in {profit, volume}."""
        if metric not in {"profit", "volume"}:
            raise ValueError("metric must be 'profit' or 'volume'")
        url = f"{LB_API}/{metric}"
        params = {"window": window, "limit": limit}
        payload = self._get_json(url, params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list from {url}, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    # The categorized leaderboard endpoint surfaces per-category rankings the
    # legacy /profit /volume endpoints don't expose. Used by the deep review
    # sweep to discover a much wider universe of skilled-looking wallets.
    LEADERBOARD_CATEGORIES: tuple[str, ...] = (
        "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
        "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE",
    )
    LEADERBOARD_TIME_PERIODS: tuple[str, ...] = ("DAY", "WEEK", "MONTH", "ALL")
    LEADERBOARD_ORDERS: tuple[str, ...] = ("PNL", "VOL")

    def get_trader_leaderboard_rankings(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One slice of the official categorized leaderboard.

        The response shape isn't formally documented; some Polymarket data-api
        endpoints return a bare list while newer ones wrap a `data` array. We
        accept either and unwrap. Caller is responsible for slicing the matrix
        (category x time_period x order_by x offset).
        """
        if category not in self.LEADERBOARD_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        if time_period not in self.LEADERBOARD_TIME_PERIODS:
            raise ValueError(f"unknown time_period {time_period!r}")
        if order_by not in self.LEADERBOARD_ORDERS:
            raise ValueError(f"unknown order_by {order_by!r}")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be in [1, 50]")
        if not 0 <= offset <= 1000:
            raise ValueError("offset must be in [0, 1000]")

        params: dict[str, Any] = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
        }
        if offset:
            params["offset"] = offset
        payload = self._get_json(f"{DATA_API}/v1/leaderboard", params=params)
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            inner = payload.get("data")
            if not isinstance(inner, list):
                raise ValueError(
                    "Expected list or {data: list} from /v1/leaderboard, "
                    f"got dict with keys {sorted(payload.keys())[:5]}"
                )
            rows = inner
        else:
            raise ValueError(
                f"Expected list or dict from /v1/leaderboard, "
                f"got {type(payload).__name__}"
            )
        return cast(list[dict[str, Any]], rows)

    # ── Per-wallet ────────────────────────────────────────────────────────────

    def get_wallet_activity(
        self,
        address: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """All trades + redemptions for a wallet, oldest-to-newest within a page."""
        params: dict[str, Any] = {"user": address, "limit": limit}
        if offset:
            params["offset"] = offset
        payload = self._get_json(f"{DATA_API}/activity", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_wallet_positions(self, address: str) -> list[dict[str, Any]]:
        """Currently-open positions for a wallet."""
        params = {"user": address}
        payload = self._get_json(f"{DATA_API}/positions", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_wallet_value(self, address: str) -> dict[str, Any]:
        """Current portfolio USD value. Returns a single-element list, we unwrap."""
        payload = self._get_json(f"{DATA_API}/value", params={"user": address})
        if not isinstance(payload, list) or not payload:
            return {"user": address.lower(), "value": 0}
        first = payload[0]
        if not isinstance(first, dict):
            raise ValueError("Expected dict in /value response")
        return cast(dict[str, Any], first)

    # ── Markets (Gamma) ───────────────────────────────────────────────────────

    def get_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,
        ascending: bool | None = None,
        condition_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if active is not None:
            params["active"] = "true" if active else "false"
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        if offset:
            params["offset"] = offset
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = "true" if ascending else "false"
        # When condition_ids is provided, httpx serializes a list as repeated params
        # (?condition_ids=a&condition_ids=b) — exactly what Gamma expects.
        if condition_ids:
            params["condition_ids"] = condition_ids
        payload = self._get_json(f"{GAMMA_API}/markets", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_markets_by_condition_ids(
        self, condition_ids: list[str], *, batch_size: int = 25
    ) -> list[dict[str, Any]]:
        """Targeted lookup. Batches because URLs can get long."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(condition_ids), batch_size):
            batch = condition_ids[i : i + batch_size]
            out.extend(self.get_markets(condition_ids=batch, limit=batch_size))
        return out

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        retryable = {429, 500, 502, 503, 504}
        for attempt in range(self._config.max_retries + 1):
            response = self._client.get(url, params=params)
            if response.status_code not in retryable:
                response.raise_for_status()
                return response.json()
            if attempt == self._config.max_retries:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(float(retry_after))
            else:
                sleep(self._config.retry_backoff_seconds * (2**attempt))
        raise RuntimeError("Polymarket retry loop exited unexpectedly")
