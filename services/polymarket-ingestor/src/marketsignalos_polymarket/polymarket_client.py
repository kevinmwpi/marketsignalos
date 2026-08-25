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
import time
from dataclasses import dataclass
from time import sleep
from typing import Any, cast

import httpx

from . import metrics

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"
GOLDSKY_SUBGRAPH = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/polymarket-orderbook-resync/prod/gn"
)

# Addresses that show up as maker/taker on the orderbook subgraph but are not
# tradeable user proxy wallets — operator/relayer/market-maker contracts that
# either don't resolve on the data-api /activity endpoint or only ever flatten.
# Empty by default; populate as concrete noise addresses are identified. The
# downstream skill model already tolerates these (a 0-activity wallet produces
# no enrichment, a flattener generates no resolved bets), so this is a cost
# optimization, not a correctness requirement.
_RECENT_TRADER_DENYLIST: frozenset[str] = frozenset()

log = logging.getLogger("marketsignalos.polymarket.client")


def _graphql_errors_are_transient(errors: Any) -> bool:
    """True when a GraphQL `errors` payload looks like a transient subgraph
    failure worth retrying — chiefly a server-side Postgres `statement_timeout`
    on an expensive query, which the subgraph returns inside an HTTP 200 body.
    A structural error (bad field, schema mismatch) is not transient and is
    surfaced immediately."""
    if not isinstance(errors, list):
        return False
    for err in errors:
        message = err.get("message", "") if isinstance(err, dict) else ""
        lowered = message.lower()
        if "statement timeout" in lowered or "canceling statement" in lowered:
            return True
    return False



def _status_outcome(status_code: int) -> str:
    """Bounded outcome label for a response status."""
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "success"


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
        user: str | None = None,
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
        if user:
            params["user"] = user
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

    # ── Recent on-chain traders (Goldsky subgraph) ──────────────────────────────

    def get_recent_trader_wallets(
        self,
        *,
        max_wallets: int = 500,
        page_size: int = 500,
        max_pages: int = 20,
    ) -> list[str]:
        """Distinct most-recently-active wallet addresses, newest-first.

        Walks the orderbook subgraph's `orderFilledEvents` in descending
        timestamp order, collecting both the maker and taker of each fill,
        until we have `max_wallets` distinct addresses, exhaust `max_pages`,
        or hit an empty page.

        Pagination uses a timestamp cursor (`where: { timestamp_lt: … }`)
        rather than `skip`, both to dodge The Graph's skip-depth ceiling on
        deep pulls and to guarantee forward progress. The trade-off is that
        fills sharing the boundary second may be skipped — acceptable here
        because we only need a fresh, representative *sample* of active
        wallets, not a complete fill log.

        The maker/taker `Account.id` is the proxy-wallet address the data-api
        `/activity?user=` endpoint accepts directly (verified in discovery),
        so no address mapping is needed. Addresses in `_RECENT_TRADER_DENYLIST`
        are dropped.
        """
        if max_wallets <= 0:
            return []
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be in [1, 1000]")

        seen: dict[str, int] = {}  # addr -> most-recent timestamp seen
        cursor_ts: int | None = None
        for _page in range(max_pages):
            where = (
                f", where: {{ timestamp_lt: {cursor_ts} }}"
                if cursor_ts is not None
                else ""
            )
            query = (
                "{ orderFilledEvents(first: %d, orderBy: timestamp, "
                "orderDirection: desc%s) { maker { id } taker { id } timestamp } }"
                % (page_size, where)
            )
            events = self._post_graphql(query).get("orderFilledEvents")
            if not isinstance(events, list) or not events:
                break

            oldest_ts: int | None = None
            for event in events:
                if not isinstance(event, dict):
                    continue
                ts = int(event.get("timestamp", 0) or 0)
                oldest_ts = ts  # events are desc-ordered; last seen is oldest
                for role in ("maker", "taker"):
                    node = event.get(role)
                    addr = str(node.get("id", "")).lower() if isinstance(node, dict) else ""
                    if not addr or addr in _RECENT_TRADER_DENYLIST:
                        continue
                    if addr not in seen or ts > seen[addr]:
                        seen[addr] = ts

            if len(seen) >= max_wallets:
                break
            if len(events) < page_size or oldest_ts is None:
                break
            cursor_ts = oldest_ts  # next page: strictly older fills

        ordered = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
        return [addr for addr, _ in ordered[:max_wallets]]

    # ── Per-wallet ────────────────────────────────────────────────────────────

    def get_wallet_activity(
        self,
        address: str,
        *,
        limit: int = 500,
        offset: int = 0,
        start: int | None = None,
        end: int | None = None,
    ) -> list[dict[str, Any]]:
        """All trades + redemptions for a wallet, oldest-to-newest within a page."""
        params: dict[str, Any] = {"user": address, "limit": limit}
        if offset:
            params["offset"] = offset
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        payload = self._get_json(f"{DATA_API}/activity", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_wallet_positions(
        self, address: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Currently-open positions for a wallet."""
        params: dict[str, Any] = {"user": address, "limit": limit}
        if offset:
            params["offset"] = offset
        payload = self._get_json(f"{DATA_API}/positions", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_wallet_closed_positions(
        self, address: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Closed or exited positions for audit and realized-performance summaries."""
        params: dict[str, Any] = {"user": address, "limit": limit}
        if offset:
            params["offset"] = offset
        payload = self._get_json(f"{DATA_API}/closed-positions", params=params)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list, got {type(payload).__name__}")
        return cast(list[dict[str, Any]], payload)

    def get_wallet_economic_summary(
        self, address: str, *, time_period: str
    ) -> dict[str, Any]:
        """Official PnL and volume summary for one wallet (VOL ranking row)."""
        rows = self.get_trader_leaderboard_rankings(
            category="OVERALL",
            time_period=time_period,
            order_by="VOL",
            limit=1,
            user=address,
        )
        return rows[0] if rows else {}

    def get_wallet_economics_for_period(
        self, address: str, *, time_period: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (pnl_row, volume_row) from separate PNL and VOL leaderboard lookups."""
        pnl_rows = self.get_trader_leaderboard_rankings(
            category="OVERALL",
            time_period=time_period,
            order_by="PNL",
            limit=1,
            user=address,
        )
        vol_rows = self.get_trader_leaderboard_rankings(
            category="OVERALL",
            time_period=time_period,
            order_by="VOL",
            limit=1,
            user=address,
        )
        return (
            pnl_rows[0] if pnl_rows else {},
            vol_rows[0] if vol_rows else {},
        )

    def get_wallet_order_fills_from_subgraph(
        self,
        address: str,
        *,
        max_pages: int = 20,
        page_size: int = 500,
        before_timestamp: int | None = None,
    ) -> list[dict[str, Any]]:
        """Wallet-specific orderbook fills from Goldsky (newest first).

        Used to extend Data API activity backfill when pagination bounds are hit.
        Rows do not include conditionId — callers use timestamps to drive further
        /activity window fetches.
        """
        wallet = address.lower()
        if not wallet:
            return []
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be in [1, 1000]")

        fills: list[dict[str, Any]] = []
        cursor_ts = before_timestamp
        for _ in range(max_pages):
            ts_filter = f", timestamp_lt: {cursor_ts}" if cursor_ts is not None else ""
            query = (
                "{ orderFilledEvents(first: %d, orderBy: timestamp, "
                'orderDirection: desc, where: { or: [{ maker: "%s" }, { taker: "%s" }]%s }) '
                "{ id timestamp transactionHash maker { id } taker { id } "
                "makerAssetId takerAssetId makerAmountFilled takerAmountFilled } }"
                % (page_size, wallet, wallet, ts_filter)
            )
            events = self._post_graphql(query).get("orderFilledEvents")
            if not isinstance(events, list) or not events:
                break
            oldest_ts: int | None = None
            for event in events:
                if isinstance(event, dict):
                    fills.append(event)
                    oldest_ts = int(event.get("timestamp", 0) or 0)
            if len(events) < page_size or oldest_ts is None:
                break
            cursor_ts = oldest_ts
        return fills

    def get_wallet_pnl_history(
        self, address: str, *, interval: str = "all", fidelity: str = "1d"
    ) -> list[dict[str, Any]]:
        """Best-effort wallet PnL time series used for drawdown diagnostics."""
        payload = self._get_json(
            f"{USER_PNL_API}/user-pnl",
            params={"user_address": address, "interval": interval, "fidelity": fidelity},
        )
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
        self,
        condition_ids: list[str],
        *,
        batch_size: int = 25,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Targeted lookup. Batches because URLs can get long."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(condition_ids), batch_size):
            batch = condition_ids[i : i + batch_size]
            out.extend(
                self.get_markets(
                    condition_ids=batch, limit=batch_size, closed=closed
                )
            )
        return out

    # ── Internals ─────────────────────────────────────────────────────────────

    def _send_instrumented(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Build and send one upstream request, timing construction and
        transport separately.

        The split is load-bearing, not cosmetic. The 2026-07-10 stall lived
        entirely inside request *construction* — httpx walks the whole cookie
        jar to build each request — while the sockets stayed fast the entire
        time. A single round-trip timer averages the two together and shows
        only "requests got slow", which is what sent that investigation after
        three wrong theories (sleep, deadlock, dead sockets). Timed apart, the
        same failure is a one-glance read on which half regressed.
        """
        host, endpoint = metrics.split_endpoint(url)
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body

        build_started = time.perf_counter()
        request = self._client.build_request(method, url, **kwargs)
        metrics.upstream_request_build_seconds.labels(host=host).observe(
            time.perf_counter() - build_started
        )

        send_started = time.perf_counter()
        try:
            response = self._client.send(request)
        except httpx.TransportError:
            metrics.upstream_request_duration_seconds.labels(
                host=host, endpoint=endpoint
            ).observe(time.perf_counter() - send_started)
            metrics.upstream_requests_total.labels(
                host=host, endpoint=endpoint, outcome="transport_error"
            ).inc()
            raise
        metrics.upstream_request_duration_seconds.labels(
            host=host, endpoint=endpoint
        ).observe(time.perf_counter() - send_started)
        metrics.upstream_requests_total.labels(
            host=host,
            endpoint=endpoint,
            outcome=_status_outcome(response.status_code),
        ).inc()
        # Sampled *before* the clear below, so the gauge reports what the
        # response actually set. Measured after, it would read zero forever and
        # silently stop guarding the regression it exists to catch.
        metrics.http_client_cookie_jar_size.set(len(self._client.cookies.jar))
        return response

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        retryable = {429, 500, 502, 503, 504}
        host, endpoint = metrics.split_endpoint(url)
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._send_instrumented("GET", url, params=params)
            except httpx.TransportError as exc:
                # Timeouts / dropped connections otherwise abort an entire
                # pipeline run. These GETs are idempotent, so retry them with
                # the same backoff as a 5xx.
                if attempt == self._config.max_retries:
                    raise
                metrics.upstream_retries_total.labels(
                    host=host, endpoint=endpoint, reason="transport_error"
                ).inc()
                log.warning(
                    "transient transport error (attempt %d/%d), retrying: %s",
                    attempt + 1,
                    self._config.max_retries,
                    exc,
                )
                sleep(self._config.retry_backoff_seconds * (2**attempt))
                continue
            # These APIs are cookie-free, but Cloudflare sets cookies on every
            # response and httpx accumulates each variant in the jar forever.
            # Request building walks the entire jar, so a large backfill
            # degrades to hours per request unless the jar stays empty.
            self._client.cookies.clear()
            if response.status_code not in retryable:
                response.raise_for_status()
                return response.json()
            if attempt == self._config.max_retries:
                response.raise_for_status()
            metrics.upstream_retries_total.labels(
                host=host,
                endpoint=endpoint,
                reason=_status_outcome(response.status_code),
            ).inc()
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(float(retry_after))
            else:
                sleep(self._config.retry_backoff_seconds * (2**attempt))
        raise RuntimeError("Polymarket retry loop exited unexpectedly")

    def _post_graphql(self, query: str) -> dict[str, Any]:
        """POST a GraphQL query to the Goldsky subgraph and return its `data`
        object. Retry semantics mirror `_get_json` (429/5xx with backoff /
        Retry-After). GraphQL surfaces query errors as HTTP 200 bodies, so we
        also raise on a populated `errors` array or a missing `data` object."""
        retryable = {429, 500, 502, 503, 504}
        host, endpoint = metrics.split_endpoint(GOLDSKY_SUBGRAPH)
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._send_instrumented(
                    "POST", GOLDSKY_SUBGRAPH, json_body={"query": query}
                )
            except httpx.TransportError as exc:
                # Read-only GraphQL query — safe to retry like _get_json.
                if attempt == self._config.max_retries:
                    raise
                metrics.upstream_retries_total.labels(
                    host=host, endpoint=endpoint, reason="transport_error"
                ).inc()
                log.warning(
                    "subgraph transient transport error (attempt %d/%d), retrying: %s",
                    attempt + 1,
                    self._config.max_retries,
                    exc,
                )
                sleep(self._config.retry_backoff_seconds * (2**attempt))
                continue
            # Same jar hygiene as _get_json — see the comment there.
            self._client.cookies.clear()
            if response.status_code not in retryable:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"Expected dict from subgraph, got {type(payload).__name__}"
                    )
                errors = payload.get("errors")
                if errors:
                    # GraphQL surfaces query errors (notably a server-side
                    # statement-timeout on an expensive query) as an HTTP 200
                    # body, so they never tripped the status-based retry above.
                    # Treat a transient timeout like a 5xx: back off and retry,
                    # raising only once retries are exhausted or for a
                    # non-transient error.
                    if (
                        _graphql_errors_are_transient(errors)
                        and attempt < self._config.max_retries
                    ):
                        metrics.upstream_retries_total.labels(
                            host=host, endpoint=endpoint, reason="graphql_timeout"
                        ).inc()
                        log.warning(
                            "subgraph transient error (attempt %d/%d), retrying: %s",
                            attempt + 1,
                            self._config.max_retries,
                            errors,
                        )
                        sleep(self._config.retry_backoff_seconds * (2**attempt))
                        continue
                    raise ValueError(f"subgraph returned errors: {errors}")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("subgraph response missing 'data' object")
                return cast(dict[str, Any], data)
            if attempt == self._config.max_retries:
                response.raise_for_status()
            metrics.upstream_retries_total.labels(
                host=host,
                endpoint=endpoint,
                reason=_status_outcome(response.status_code),
            ).inc()
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(float(retry_after))
            else:
                sleep(self._config.retry_backoff_seconds * (2**attempt))
        raise RuntimeError("Polymarket subgraph retry loop exited unexpectedly")
