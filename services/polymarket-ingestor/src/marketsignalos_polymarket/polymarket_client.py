"""
Sync HTTP client for Polymarket's public APIs.

All endpoints are unauthenticated. We hit four hostnames:
  - lb-api.polymarket.com   (leaderboards)
  - data-api.polymarket.com (per-wallet activity, positions, value; v2 routes)
  - gamma-api.polymarket.com (market metadata)
  - api.goldsky.com         (subgraph for backfill — not yet wired)

The Data API is v2 (``/v2/...``): v1 has answered HTTP 410 since 2026-11-02.
v2 wraps every payload in ``data``, names fields in snake_case and pages
listings by an opaque cursor instead of an offset
(docs/benchmarks/2026-11-17-data-api-v2-probe.md).

Retry semantics mirror the Kalshi client: exponential backoff on 429/5xx,
respects Retry-After when present.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
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



def _plain_decimal(value: float) -> str:
    """``value`` without an exponent: a small float would otherwise go on the
    wire as ``1e-06``, which not every query-string parser reads as a number."""
    return format(Decimal(str(value)), "f")


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
class DataPage:
    """One page of a Data API v2 listing; ``next_cursor`` is None on the last page."""

    rows: list[dict[str, Any]]
    next_cursor: str | None


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
    LEADERBOARD_MAX_LIMIT = 1000

    def get_trader_leaderboard_page(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        limit: int = 50,
        cursor: str | None = None,
    ) -> DataPage:
        """One page of the official categorized leaderboard, best rank first.

        Rows carry ``user_id`` (the proxy wallet), ``user_name``, ``rank``,
        ``pnl`` in USDC and ``volume`` in shares. v1 reported volume in USDC:
        a wallet's USDC volume now comes from ``get_wallet_volume``. Caller is
        responsible for slicing the matrix (category x time_period x order_by)
        and for following ``next_cursor``.
        """
        self._check_leaderboard_slice(category, time_period)
        if order_by not in self.LEADERBOARD_ORDERS:
            raise ValueError(f"unknown order_by {order_by!r}")
        if not 1 <= limit <= self.LEADERBOARD_MAX_LIMIT:
            raise ValueError(f"limit must be in [1, {self.LEADERBOARD_MAX_LIMIT}]")
        params: dict[str, Any] = {
            "category": category,
            "time_period": time_period.lower(),
            # "VOL" is this codebase's name for the ordering; v2 rejects it.
            "sort_by": "VOLUME" if order_by == "VOL" else order_by,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self._get_data_page("/v2/leaderboard", params)

    def get_wallet_leaderboard_standing(
        self, address: str, *, time_period: str, category: str = "OVERALL"
    ) -> dict[str, Any]:
        """A wallet's official PnL (USDC) and volume (shares) for one period.

        Empty when the leaderboard has no standing for the wallet.
        """
        self._check_leaderboard_slice(category, time_period)
        data = self._get_data(
            "/v2/leaderboard",
            {"user": address, "category": category, "time_period": time_period.lower()},
        )
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Expected an object or null in /v2/leaderboard data, got {type(data).__name__}"
            )
        return cast(dict[str, Any], data)

    def _check_leaderboard_slice(self, category: str, time_period: str) -> None:
        if category not in self.LEADERBOARD_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        if time_period not in self.LEADERBOARD_TIME_PERIODS:
            raise ValueError(f"unknown time_period {time_period!r}")

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
            # Percent-format is deliberate: GraphQL is brace-dense, and an
            # f-string would need every literal { and } doubled. This keeps the
            # query readable as the GraphQL it actually is.
            query = (
                "{ orderFilledEvents(first: %d, orderBy: timestamp, "  # noqa: UP031
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

    def get_wallet_activity_page(
        self,
        address: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> DataPage:
        """One page of a wallet's trades, redemptions and other events, newest first.

        ``start`` and ``end`` are inclusive epoch seconds. Without ``start`` v2
        serves only a recent window, so the default asks for all history.
        Deposits and withdrawals are left out, as they were in v1. A cursor is
        only valid with the filters and limit of the page that issued it.
        """
        params: dict[str, Any] = {
            "user": address,
            "limit": limit,
            "sort_direction": "DESC",
            "start": 1 if start is None else start,
            "exclude_deposits_withdrawals": "true",
        }
        if end is not None:
            params["end"] = end
        if cursor:
            params["cursor"] = cursor
        return self._get_data_page("/v2/activity", params)

    def get_wallet_positions_page(
        self,
        address: str,
        *,
        status: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
        min_shares: float | None = None,
        min_value_usdc: float | None = None,
        sort_by: str | None = None,
    ) -> DataPage:
        """One page of a wallet's positions.

        Without ``status`` the listing is every current holding, largest value
        first. It includes holdings in resolved markets that the wallet has
        not redeemed (``redeemable``), worthless ones too, and v2 has no
        parameter that leaves them out. ``status="CLOSED"`` lists exited
        positions (v1 ``/closed-positions``), largest realized PnL first.

        One floor per request: ``min_shares`` on the holding's size (the
        server's own floor is 0.1 shares and cannot be lowered) or
        ``min_value_usdc`` on its current value. ``sort_by`` orders descending.
        A cursor is bound to the status and ordering of the page that issued it.
        """
        if min_shares is not None and min_value_usdc is not None:
            raise ValueError("v2 takes one position floor per request: shares or value")
        params: dict[str, Any] = {"user": address, "limit": limit}
        if status is not None:
            params["status"] = status
        if min_shares is not None:
            params["filter_type"] = "TOKENS"
            params["filter_amount"] = _plain_decimal(min_shares)
        if min_value_usdc is not None:
            params["filter_type"] = "CASH"
            params["filter_amount"] = _plain_decimal(min_value_usdc)
        if sort_by is not None:
            params["sort_by"] = sort_by
            params["sort_direction"] = "DESC"
        if cursor:
            params["cursor"] = cursor
        return self._get_data_page("/v2/positions", params)

    def get_wallet_volume(self, address: str) -> dict[str, Any]:
        """All-time trading volume: ``volume`` in shares, ``volume_usdc`` and ``trade_count``."""
        data = self._get_data("/v2/user-volume", {"user": address, "start": 1})
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Expected an object or null in /v2/user-volume data, got {type(data).__name__}"
            )
        return cast(dict[str, Any], data)

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
            # Percent-format for the same reason as above: doubling braces in
            # an f-string would make this query far harder to read.
            query = (
                "{ orderFilledEvents(first: %d, orderBy: timestamp, "  # noqa: UP031
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
        """Current portfolio USD value: ``{"proxy_wallet", "value"}``."""
        data = self._get_data("/v2/value", {"user": address})
        if data is None:
            return {"proxy_wallet": address.lower(), "value": 0}
        if not isinstance(data, dict):
            raise ValueError(
                f"Expected an object or null in /v2/value data, got {type(data).__name__}"
            )
        return cast(dict[str, Any], data)

    # ── Markets (Gamma) ───────────────────────────────────────────────────────

    def get_markets_once(
        self, condition_ids: list[str], *, closed: bool, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """One transport attempt. The bounded backfill owns durable retry scheduling."""
        if not condition_ids or len(condition_ids) > 25 or not 1 <= limit <= 100:
            raise ValueError("A targeted request needs 1-25 conditions and a limit of 1-100")
        try:
            response = self._send_instrumented(
                "GET", f"{GAMMA_API}/markets",
                params={"condition_ids": condition_ids, "closed": str(closed).lower(),
                        "limit": limit},
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            self._client.cookies.clear()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError("Expected a list of market objects")
        return cast(list[dict[str, Any]], payload)

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

    def _get_data(self, path: str, params: dict[str, Any]) -> Any:
        """The ``data`` member of a Data API v2 response."""
        payload = self._get_json(f"{DATA_API}{path}", params=params)
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError(f"Expected {{data: ...}} from {path}, got {type(payload).__name__}")
        return payload["data"]

    def _get_data_page(self, path: str, params: dict[str, Any]) -> DataPage:
        """One page of a v2 listing: ``{data: [...], pagination: {has_more, next_cursor}}``."""
        payload = self._get_json(f"{DATA_API}{path}", params=params)
        rows = payload.get("data") if isinstance(payload, dict) else None
        pagination = payload.get("pagination") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not isinstance(pagination, dict):
            raise ValueError(
                f"Expected {{data: [...], pagination: {{...}}}} from {path}, "
                f"got {type(payload).__name__}"
            )
        cursor = pagination.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError(f"Invalid next_cursor from {path}")
        # A page that says more rows exist but gives no way to reach them would
        # silently truncate a history: fail instead.
        if pagination.get("has_more") and cursor is None:
            raise ValueError(f"{path} reports has_more without a next_cursor")
        return DataPage(rows=cast(list[dict[str, Any]], rows), next_cursor=cursor)

    def _send_instrumented(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        follow_redirects: bool | None = None,
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
            response = (self._client.send(request) if follow_redirects is None
                        else self._client.send(request, follow_redirects=follow_redirects))
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
