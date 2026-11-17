"""An in-memory stand-in for Polymarket's Data API v2, for ``httpx.MockTransport``.

It answers the routes the collector reads with v2's envelope and cursor paging,
as the live probe recorded them (docs/benchmarks/2026-11-17-data-api-v2-probe.md):

- listings are ``{"data": [...], "pagination": {"has_more", "next_cursor"}}``;
- a cursor keeps the page size of the request that issued it;
- ``offset`` is answered with HTTP 400, as is ``sort_by=VOL``;
- ``start`` and ``end`` are inclusive epoch seconds;
- a wallet with no standing in a period is ``{"data": null}``.
"""
from __future__ import annotations

from typing import Any

import httpx

HOST = "data-api.polymarket.com"


def activity_row(
    ts: int, tx: str, cond: str = "0xc", oi: int = 0, *, wallet: str = "0xabc",
    **extra: Any,
) -> dict[str, Any]:
    """One TRADE row as /v2/activity serves it."""
    return {
        "proxy_wallet": wallet,
        "timestamp": ts,
        "condition_id": cond,
        "type": "TRADE",
        "size": 1.0,
        "usdc_size": 0.5,
        "transaction_hash": tx,
        "price": 0.5,
        "token_id": "1",
        "side": "BUY",
        "outcome_index": oi,
        "title": "t",
        "slug": "s",
        "icon": "",
        "event_slug": "e",
        "outcome": "Yes",
        "name": "",
        "pseudonym": "",
        "bio": "",
        "profile_image": "",
        "profile_image_optimized": "",
        **extra,
    }


def position_row(cond: str, oi: int = 0, *, size: float = 10.0, **extra: Any) -> dict[str, Any]:
    """One holding as /v2/positions serves it (the fields the collector reads)."""
    return {
        "token_id": f"token-{cond}-{oi}",
        "condition_id": cond,
        "outcome_index": oi,
        "outcome": "Yes",
        "current_size": size,
        "avg_price": 0.4,
        "current_value": size * 0.5,
        "current_price": 0.5,
        "unrealized_pnl": size * 0.1,
        "realized_pnl": 0.0,
        "status": "OPEN",
        "redeemable": False,
        "title": "t",
        "slug": "s",
        "event_slug": "e",
        **extra,
    }


def leaderboard_row(wallet: str, *, pnl: float = 1.0, name: str = "n") -> dict[str, Any]:
    """One row as /v2/leaderboard serves it. ``volume`` is in shares there."""
    return {
        "rank": 1, "user_id": wallet, "user_name": name, "pnl": pnl, "volume": 2.0,
        "profile_image": "", "x_username": "", "verified": False,
    }


def page(rows: list[dict[str, Any]], next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": rows,
        "pagination": {
            "limit": len(rows), "offset": 0,
            "has_more": next_cursor is not None, "next_cursor": next_cursor,
        },
    }


class FakeDataApi:
    """Rows in, v2 responses out. ``requests`` records every Data API call."""

    def __init__(self) -> None:
        self.activity: dict[str, list[dict[str, Any]]] = {}
        self.positions: dict[str, list[dict[str, Any]]] = {}
        self.closed_positions: dict[str, list[dict[str, Any]]] = {}
        # (category, time_period, sort_by) → rows; a missing board is ``default_board``.
        self.leaderboards: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self.default_board: list[dict[str, Any]] = []
        # (wallet, time_period) → standing; a missing one is ``data: null``.
        self.standings: dict[tuple[str, str], dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.values: dict[str, float] = {}
        # path → HTTP status to answer instead (an upstream fault).
        self.failures: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    def calls(self, path: str) -> list[dict[str, str]]:
        """Query parameters of every request made to ``path``, in order."""
        return [dict(r.url.params) for r in self.requests if r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != HOST:
            return httpx.Response(404)
        self.requests.append(request)
        path = request.url.path
        params = request.url.params
        if path in self.failures:
            return httpx.Response(self.failures[path], json={"error": "fault"})
        if not path.startswith("/v2/"):
            return httpx.Response(410, json={"error": "Data API v1 has been retired"})
        if "offset" in params:
            return httpx.Response(400, json={"error": "'offset' is not a query param"})
        wallet = (params.get("user") or "").lower()
        if path == "/v2/activity":
            start = int(params.get("start") or 0)
            end = int(params["end"]) if params.get("end") else None
            rows = sorted(
                (
                    row for row in self.activity.get(wallet, [])
                    if row["timestamp"] >= start and (end is None or row["timestamp"] <= end)
                ),
                key=lambda row: -row["timestamp"],  # stable: equal seconds keep their order
            )
            return self._listing(rows, params)
        if path == "/v2/positions":
            if params.get("status") == "CLOSED":
                return self._listing(self.closed_positions.get(wallet, []), params)
            held = [r for r in self.positions.get(wallet, []) if r["current_size"] >= 0.1]
            if params.get("filter_type") == "CASH":
                floor = float(params.get("filter_amount") or 0)
                rows = [r for r in held if r["current_value"] >= floor]
            else:
                floor = float(params.get("filter_amount") or 0.1)
                rows = [r for r in held if r["current_size"] >= floor]
            if params.get("sort_by") == "TOKENS":
                rows.sort(key=lambda row: -row["current_size"])
            return self._listing(rows, params)
        if path == "/v2/leaderboard":
            period = params.get("time_period") or "day"
            if params.get("user"):
                standing = self.standings.get((wallet, period))
                return httpx.Response(200, json={"data": standing})
            sort_by = params.get("sort_by") or "PNL"
            if sort_by not in {"PNL", "VOLUME"}:
                return httpx.Response(400, json={"error": "sort_by must be PNL or VOLUME"})
            board = self.leaderboards.get(
                (params.get("category") or "OVERALL", period, sort_by), self.default_board)
            return self._listing(board, params)
        if path == "/v2/user-volume":
            return httpx.Response(200, json={"data": self.volumes.get(wallet)})
        if path == "/v2/value":
            return httpx.Response(200, json={"data": {
                "proxy_wallet": wallet, "value": self.values.get(wallet, 0.0)}})
        return httpx.Response(404)

    @staticmethod
    def _listing(rows: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
        cursor = params.get("cursor")
        if cursor:
            try:
                index, limit = (int(part) for part in cursor.split(":"))
            except ValueError:
                return httpx.Response(400, json={"error": "invalid cursor"})
        else:
            index, limit = 0, int(params.get("limit") or 100)
        if not 0 < limit <= 1000:
            return httpx.Response(400, json={"error": "limit must be between 0 and 1000"})
        served = rows[index:index + limit]
        more = index + limit < len(rows)
        return httpx.Response(200, json=page(served, f"{index + limit}:{limit}" if more else None))
