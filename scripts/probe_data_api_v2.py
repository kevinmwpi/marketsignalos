"""Live probe of Polymarket's Data API v2 (v1 answers HTTP 410 since 2026-11-02).

Records what the migration in ``polymarket_client.py`` depends on: routes, the
``data``/``pagination`` envelope, cursor paging, time-window semantics, row
fields per activity type, position statuses, and the leaderboard's units.

Rows of the ``--active`` wallet are reduced to shapes (types, lengths, enum-like
values) so no recent price, size or PnL is written. Full rows are kept only for
``--legacy``, a wallet whose history ended in 2024, to seed test fixtures.

Usage (network needed: data-api.polymarket.com)::

    uv run --no-sync python scripts/probe_data_api_v2.py --out probe.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import httpx

BASE_URL = "https://data-api.polymarket.com"
# Theo4: named in docs/polymarket-api-discovery.md; no activity since 2024.
LEGACY_WALLET = "0x56687bf447db6ffa42ffe2204a05edaa20f55839"
UNKNOWN_WALLET = "0x0000000000000000000000000000000000000001"
USER_AGENT = "MarketSignalOS-polymarket/0.1"
ENUM_KEYS = frozenset({"type", "side", "status", "is_combo", "redeemable", "mergeable",
                       "negative_risk", "archived", "verified", "outcome_index"})
HEX_KEYS = frozenset({"transaction_hash", "condition_id", "proxy_wallet", "user_id",
                      "token_id", "opposite_token_id"})
HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after", "cache-control", "content-type",
                   "deprecation", "sunset", "link")


class Probe:
    def __init__(self, pause: float) -> None:
        self._client = httpx.Client(timeout=20.0, follow_redirects=True, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
        self._pause = pause
        self.requests = 0
        self.statuses: Counter[int] = Counter()

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """One GET. Never raises on an HTTP status: errors are part of the record."""
        time.sleep(self._pause)
        self.requests += 1
        started = time.perf_counter()
        try:
            response = self._client.get(BASE_URL + path, params={
                k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            return {"path": path, "params": params, "error": type(exc).__name__, "detail": str(exc)}
        self._client.cookies.clear()
        self.statuses[response.status_code] += 1
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        headers = {k: v for k, v in response.headers.items()
                   if k.lower().startswith(HEADER_PREFIXES)}
        return {"path": path, "params": params, "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000), "headers": headers,
                "body": body, "text": None if body is not None else response.text[:400]}


def rows_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    body = result.get("body")
    data = body.get("data") if isinstance(body, dict) else body
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def page_of(result: dict[str, Any]) -> dict[str, Any]:
    body = result.get("body")
    page = body.get("pagination") if isinstance(body, dict) else None
    if not isinstance(page, dict):
        return {}
    cursor = page.get("next_cursor")
    return {**{k: v for k, v in page.items() if k != "next_cursor"},
            "next_cursor": None if cursor is None else f"str({len(str(cursor))})"}


def cursor_of(result: dict[str, Any]) -> str | None:
    body = result.get("body")
    page = body.get("pagination") if isinstance(body, dict) else None
    cursor = page.get("next_cursor") if isinstance(page, dict) else None
    return cursor if isinstance(cursor, str) and cursor else None


def describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return f"int({len(str(abs(value)))} digits)"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        if re.fullmatch(r"0x[0-9a-f]+", value):
            return f"hex-lower({len(value)})"
        if re.fullmatch(r"0x[0-9a-fA-F]+", value):
            return f"hex-mixed({len(value)})"
        if re.fullmatch(r"-?[0-9]+", value):
            return "str-int"
        if re.fullmatch(r"-?[0-9]*\.[0-9]+", value):
            return "str-decimal"
        return "str" if value else "str-empty"
    return type(value).__name__


def shape(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per key: value kinds with counts, and the values of enum-like keys."""
    kinds: dict[str, Counter[str]] = {}
    enums: dict[str, Counter[str]] = {}
    for row in rows:
        for key, value in row.items():
            kinds.setdefault(key, Counter())[describe(value)] += 1
            if key in ENUM_KEYS:
                enums.setdefault(key, Counter())[json.dumps(value)] += 1
    return {"rows": len(rows),
            "keys": {key: dict(counts.most_common(6)) for key, counts in sorted(kinds.items())},
            "absent": {key: len(rows) - sum(kinds[key].values()) for key in sorted(kinds)
                       if sum(kinds[key].values()) < len(rows)},
            "enum_values": {key: dict(counts.most_common(25)) for key, counts in sorted(enums.items())}}


def summary(result: dict[str, Any], *, keep_rows: bool = False) -> dict[str, Any]:
    """A request's record without row values unless ``keep_rows`` (legacy wallet only)."""
    rows = rows_of(result)
    body = result.get("body")
    out: dict[str, Any] = {k: result.get(k) for k in ("path", "params", "status", "ms", "headers")
                           if result.get(k) is not None}
    out["top_level_keys"] = sorted(body) if isinstance(body, dict) else describe(body)
    out["pagination"] = page_of(result)
    out["shape"] = shape(rows)
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        out["data_object_shape"] = {k: describe(v) for k, v in sorted(body["data"].items())}
    if isinstance(body, dict) and "data" in body and body["data"] is None:
        out["data_is_null"] = True
    if result.get("status") != 200 or result.get("error"):
        out["error_body"] = body if body is not None else result.get("text") or result.get("detail")
    if keep_rows:
        out["rows"] = rows
    return out


def timestamps(rows: list[dict[str, Any]]) -> list[int]:
    return [int(row["timestamp"]) for row in rows if isinstance(row.get("timestamp"), (int, float))]


def activity_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """The store's dedupe key (storage.JsonlActivityStore._dedupe_key)."""
    return (row.get("transaction_hash"), row.get("condition_id"), row.get("outcome_index"),
            row.get("type"))


def walk(probe: Probe, path: str, pages: int, *, keep_collisions: bool = False,
         **params: Any) -> dict[str, Any]:
    """Follow ``next_cursor`` for ``pages`` pages; report order, overlap and per-type shapes.

    ``keep_collisions`` adds up to three groups of rows that share one dedupe key
    (legacy wallet only): whether v2 splits what the store treats as one row.
    """
    rows: list[dict[str, Any]] = []
    sizes: list[int] = []
    cursor: str | None = None
    statuses: list[int | None] = []
    for _ in range(pages):
        result = probe.get(path, **params, cursor=cursor)
        statuses.append(result.get("status"))
        page_rows = rows_of(result)
        sizes.append(len(page_rows))
        rows.extend(page_rows)
        cursor = cursor_of(result)
        if cursor is None:
            break
    stamps = timestamps(rows)
    keys = Counter(activity_key(row) for row in rows)
    identical = Counter(json.dumps(row, sort_keys=True) for row in rows)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(str(row.get("type")), []).append(row)
    same_second = Counter(stamps)
    groups = [[row for row in rows if activity_key(row) == key]
              for key, n in keys.items() if n > 1][:3] if keep_collisions else []
    return {
        "collision_groups": groups,
        "params": params, "statuses": statuses, "page_sizes": sizes, "rows": len(rows),
        "has_more_after_last_page": cursor is not None,
        "timestamps_non_increasing": all(a >= b for a, b in pairwise(stamps)),
        "timestamps_non_decreasing": all(a <= b for a, b in pairwise(stamps)),
        "newest": max(stamps) if stamps else None, "oldest": min(stamps) if stamps else None,
        "max_rows_sharing_one_second": max(same_second.values()) if same_second else 0,
        "identical_rows_repeated": sum(n - 1 for n in identical.values() if n > 1),
        "dedupe_key_collisions": sum(n - 1 for n in keys.values() if n > 1),
        "collision_examples_by_type": dict(Counter(
            str(key[3]) for key, n in keys.items() if n > 1).most_common(8)),
        "types": dict(Counter(str(row.get("type")) for row in rows).most_common()),
        "shape_by_type": {name: shape(group) for name, group in sorted(by_type.items())},
    }


def probe_activity(probe: Probe, wallet: str, *, keep_rows: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    default = probe.get("/v2/activity", user=wallet, limit=5)
    out["default_first_page"] = summary(default, keep_rows=keep_rows)
    out["default_timestamps"] = timestamps(rows_of(default))
    oldest_default = probe.get("/v2/activity", user=wallet, limit=1, sort_direction="ASC")
    out["default_window_oldest"] = timestamps(rows_of(oldest_default))
    oldest_full = probe.get("/v2/activity", user=wallet, limit=3, sort_direction="ASC", start=1)
    out["full_history_oldest"] = summary(oldest_full, keep_rows=keep_rows)
    out["full_history_oldest_timestamps"] = timestamps(rows_of(oldest_full))
    out["walk_desc_full_history"] = walk(probe, "/v2/activity", 3, keep_collisions=keep_rows,
                                         user=wallet, start=1, limit=500)
    out["walk_default_window"] = walk(probe, "/v2/activity", 2, user=wallet, limit=500)

    first = rows_of(probe.get("/v2/activity", user=wallet, start=1, limit=40))
    stamps = timestamps(first)
    if len(stamps) >= 10:
        mid = stamps[len(stamps) // 2]
        at_end = timestamps(rows_of(probe.get("/v2/activity", user=wallet, start=1, end=mid, limit=5)))
        before = timestamps(rows_of(
            probe.get("/v2/activity", user=wallet, start=1, end=mid - 1, limit=5)))
        same = timestamps(rows_of(
            probe.get("/v2/activity", user=wallet, start=mid, end=mid, limit=500)))
        from_start = timestamps(rows_of(probe.get(
            "/v2/activity", user=wallet, start=mid, limit=500, sort_direction="ASC")))
        out["window_bounds"] = {
            "mid": mid, "rows_at_mid_in_first_page": stamps.count(mid),
            "end_equals_mid_newest": max(at_end) if at_end else None,
            "end_is_inclusive": bool(at_end) and max(at_end) == mid,
            "end_minus_one_newest": max(before) if before else None,
            "start_and_end_equal_rows": len(same),
            "start_and_end_equal_all_at_mid": bool(same) and set(same) == {mid},
            "start_equals_mid_oldest": min(from_start) if from_start else None,
            "start_is_inclusive": bool(from_start) and min(from_start) == mid,
        }
    filters: dict[str, dict[str, Any]] = {
        "type_trade": {"type": "TRADE"}, "type_trade_redeem": {"type": "TRADE,REDEEM"},
        "side_buy": {"side": "BUY"},
        "exclude_deposits_true": {"exclude_deposits_withdrawals": "true"},
        "exclude_deposits_false": {"exclude_deposits_withdrawals": "false"},
    }
    for name, extra in filters.items():
        result = probe.get("/v2/activity", user=wallet, start=1, limit=500, **extra)
        rows = rows_of(result)
        out[name] = {"status": result.get("status"), "rows": len(rows),
                     "types": dict(Counter(str(row.get("type")) for row in rows).most_common()),
                     "sides": dict(Counter(json.dumps(row.get("side")) for row in rows)),
                     "error_body": None if result.get("status") == 200 else result.get("body")}
    for limit in (1000, 1001):
        result = probe.get("/v2/activity", user=wallet, start=1, limit=limit)
        out[f"limit_{limit}"] = {"status": result.get("status"), "rows": len(rows_of(result)),
                                 "error_body": None if result.get("status") == 200
                                 else result.get("body") or result.get("text")}
    small = probe.get("/v2/activity", user=wallet, start=1, limit=5)
    cursor = cursor_of(small)
    if cursor is not None:
        other_limit = probe.get("/v2/activity", user=wallet, start=1, limit=7, cursor=cursor)
        other_filter = probe.get("/v2/activity", user=wallet, start=1, limit=5, cursor=cursor,
                                 type="TRADE")
        out["cursor_reuse"] = {
            "with_other_limit": {"status": other_limit.get("status"),
                                 "rows": len(rows_of(other_limit)),
                                 "error_body": None if other_limit.get("status") == 200
                                 else other_limit.get("body")},
            "with_other_filter": {"status": other_filter.get("status"),
                                  "error_body": None if other_filter.get("status") == 200
                                  else other_filter.get("body")},
        }
    rejected: dict[str, dict[str, Any]] = {
        "offset_param": {"offset": 5}, "bad_cursor": {"cursor": "not-a-cursor"},
        "v1_style_market_param": {"market": "0x" + "00" * 32}}
    for name, extra in rejected.items():
        result = probe.get("/v2/activity", user=wallet, start=1, limit=5, **extra)
        out[name] = {"status": result.get("status"), "rows": len(rows_of(result)),
                     "body_if_error": None if result.get("status") == 200
                     else result.get("body") or result.get("text")}
    return out


def position_keys(rows: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
    return {(row.get("condition_id"), row.get("outcome_index")) for row in rows}


def probe_positions(probe: Probe, wallet: str, *, keep_rows: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    listings: dict[str, list[dict[str, Any]]] = {}
    variants: dict[str, dict[str, Any]] = {
        "default": {}, "open": {"status": "OPEN"}, "redeemable": {"status": "REDEEMABLE"},
        "redeemable_lost": {"status": "REDEEMABLE_LOST"}, "mergeable": {"status": "MERGEABLE"},
        "closed": {"status": "CLOSED"}, "open_and_redeemable": {"status": "OPEN,REDEEMABLE"},
        "tokens_at_least_one": {"filter_type": "TOKENS", "filter_amount": 1},
        "include_archived": {"include_archived": "true"},
    }
    for name, extra in variants.items():
        result = probe.get("/v2/positions", user=wallet, limit=500, **extra)
        rows = rows_of(result)
        listings[name] = rows
        sizes = [float(row["current_size"]) for row in rows
                 if isinstance(row.get("current_size"), (int, float, str)) and row["current_size"] != ""]
        record = summary(result, keep_rows=keep_rows and name in {"default", "closed"})
        if keep_rows and "rows" in record:
            record["rows"] = record["rows"][:4]
        record["statuses_reported"] = dict(Counter(str(row.get("status")) for row in rows))
        record["current_size_below_one"] = sum(size < 1 for size in sizes)
        record["current_size_zero"] = sum(size == 0 for size in sizes)
        out[name] = record
    default, opened, redeemable = (position_keys(listings[n]) for n in ("default", "open", "redeemable"))
    out["default_vs_statuses"] = {
        "default": len(default), "open": len(opened), "redeemable": len(redeemable),
        "default_equals_open": default == opened,
        "default_equals_open_plus_redeemable": default == (opened | redeemable),
        "redeemable_inside_default": len(redeemable & default),
        "note": "only the first page (500) of each listing is compared",
    }
    paged = walk(probe, "/v2/positions", 2, user=wallet, limit=5)
    out["cursor_walk_limit_5"] = {k: paged[k] for k in ("statuses", "page_sizes", "rows",
                                                        "has_more_after_last_page",
                                                        "identical_rows_repeated")}
    return out


def probe_leaderboard(probe: Probe) -> tuple[dict[str, Any], str | None]:
    out: dict[str, Any] = {}
    variants: dict[str, dict[str, Any]] = {
        "month_pnl": {"category": "OVERALL", "time_period": "month", "sort_by": "PNL"},
        "month_volume": {"category": "OVERALL", "time_period": "month", "sort_by": "VOLUME"},
        "all_pnl": {"category": "OVERALL", "time_period": "all", "sort_by": "PNL"},
        "no_params": {},
        "v1_style_params": {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL"},
        "uppercase_period": {"category": "OVERALL", "time_period": "MONTH", "sort_by": "PNL"},
        "v1_sort_name": {"category": "OVERALL", "time_period": "month", "sort_by": "VOL"},
        "lowercase_category": {"category": "overall", "time_period": "month", "sort_by": "PNL"},
        "category_politics": {"category": "POLITICS", "time_period": "month", "sort_by": "PNL"},
    }
    top: str | None = None
    for name, params in variants.items():
        result = probe.get("/v2/leaderboard", limit=5, **params)
        record = summary(result)
        rows = rows_of(result)
        record["ranks"] = [row.get("rank") for row in rows]
        record["pnl_non_increasing"] = all(
            float(a["pnl"]) >= float(b["pnl"]) for a, b in pairwise(rows)
            if "pnl" in a and "pnl" in b)
        out[name] = record
        if name == "month_pnl" and rows:
            top = str(rows[0].get("user_id") or "") or None
    base = variants["month_pnl"]
    one = rows_of(probe.get("/v2/leaderboard", limit=100, **base))
    first = probe.get("/v2/leaderboard", limit=50, **base)
    second = probe.get("/v2/leaderboard", limit=50, cursor=cursor_of(first), **base)
    two = rows_of(first) + rows_of(second)
    out["paging_100_vs_2x50"] = {
        "one_page_rows": len(one), "two_page_rows": len(two),
        "same_wallet_order": [r.get("user_id") for r in one] == [r.get("user_id") for r in two],
        "distinct_wallets_one_page": len({r.get("user_id") for r in one}),
        "second_page_status": second.get("status"),
        "ranks_first_last": [one[0].get("rank"), one[-1].get("rank")] if one else None,
    }
    for limit in (1000, 1001):
        result = probe.get("/v2/leaderboard", limit=limit, **base)
        out[f"limit_{limit}"] = {"status": result.get("status"), "rows": len(rows_of(result)),
                                 "error_body": None if result.get("status") == 200
                                 else result.get("body") or result.get("text")}
    return out, top


def probe_wallet_scalars(probe: Probe, wallet: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["value"] = summary(probe.get("/v2/value", user=wallet))
    for period in ("all", "month"):
        out[f"standing_{period}"] = summary(
            probe.get("/v2/leaderboard", user=wallet, category="OVERALL", time_period=period))
    out["standing_no_category"] = summary(probe.get("/v2/leaderboard", user=wallet, time_period="all"))
    out["user_volume_full_history"] = summary(probe.get("/v2/user-volume", user=wallet, start=1))
    out["user_volume_default"] = summary(probe.get("/v2/user-volume", user=wallet))
    out["user_stats"] = summary(probe.get("/v2/user-stats", user=wallet))
    standing = probe.get("/v2/leaderboard", user=wallet, category="OVERALL", time_period="all")
    volume = probe.get("/v2/user-volume", user=wallet, start=1)
    try:
        shares = float(standing["body"]["data"]["volume"])
        own_shares = float(volume["body"]["data"]["volume"])
        own_usdc = float(volume["body"]["data"]["volume_usdc"])
        # Ratios only: whether the leaderboard's volume is the shares or the USDC figure.
        out["leaderboard_volume_unit"] = {
            "standing_over_user_volume_shares": round(shares / own_shares, 4) if own_shares else None,
            "standing_over_user_volume_usdc": round(shares / own_usdc, 4) if own_usdc else None,
        }
    except (KeyError, TypeError, ValueError):
        out["leaderboard_volume_unit"] = "not comparable"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--legacy", default=LEGACY_WALLET)
    parser.add_argument("--active", default="", help="default: rank 1 by month PnL")
    parser.add_argument("--pause", type=float, default=0.35, help="seconds between requests")
    args = parser.parse_args(argv)

    probe = Probe(args.pause)
    started = datetime.now(UTC).isoformat()
    report: dict[str, Any] = {"schema_version": 1, "base_url": BASE_URL, "started_at": started,
                              "legacy_wallet": args.legacy}
    report["v1_routes_now"] = {
        name: {"status": result.get("status"), "error": result.get("error"),
               "body": result.get("body") if result.get("body") is not None else result.get("text")}
        for name, result in {
            "activity": probe.get("/activity", user=args.legacy, limit=1),
            "positions": probe.get("/positions", user=args.legacy, limit=1),
            "closed_positions": probe.get("/closed-positions", user=args.legacy, limit=1),
            "value": probe.get("/value", user=args.legacy),
            "leaderboard": probe.get("/v1/leaderboard", limit=1),
            "trades": probe.get("/trades", user=args.legacy, limit=1),
        }.items()}
    report["leaderboard"], top = probe_leaderboard(probe)
    active = (args.active or top or "").lower()
    report["active_wallet_is_rank_1_month_pnl"] = bool(active) and not args.active
    report["legacy"] = {
        "activity": probe_activity(probe, args.legacy, keep_rows=True),
        "positions": probe_positions(probe, args.legacy, keep_rows=True),
        "scalars": probe_wallet_scalars(probe, args.legacy),
    }
    if active:
        report["active"] = {
            "activity": probe_activity(probe, active, keep_rows=False),
            "positions": probe_positions(probe, active, keep_rows=False),
            "scalars": probe_wallet_scalars(probe, active),
        }
    report["unknown_wallet"] = {
        "activity": summary(probe.get("/v2/activity", user=UNKNOWN_WALLET, start=1, limit=5)),
        "positions": summary(probe.get("/v2/positions", user=UNKNOWN_WALLET, limit=5)),
        "value": summary(probe.get("/v2/value", user=UNKNOWN_WALLET)),
        "standing": summary(probe.get("/v2/leaderboard", user=UNKNOWN_WALLET, time_period="all")),
        "no_user": summary(probe.get("/v2/activity", limit=5)),
        "uppercase_address": summary(probe.get(
            "/v2/activity", user="0x" + args.legacy[2:].upper(), start=1, limit=2)),
    }
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["requests"] = probe.requests
    report["status_counts"] = {str(code): n for code, n in sorted(probe.statuses.items())}
    with open(args.out, "w", encoding="utf-8") as out:
        json.dump(report, out, indent=1, sort_keys=True)
        out.write("\n")
    sys.stdout.write(f"wrote {args.out}: {probe.requests} requests, "
                     f"statuses {report['status_counts']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
