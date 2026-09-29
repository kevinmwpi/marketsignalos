"""Read-only probe: can closing prices be backfilled for resolved Polymarket markets?

Closing-line value (CLV) needs each market's price shortly before it closed. The
old local price snapshots cover about a day and a half, so either the history is
recoverable from Polymarket's CLOB ``/prices-history`` endpoint or it has to be
collected going forward. This probe measures, on real markets across the whole
history:

  1. which granularities (``fidelity``, minutes) return data for resolved markets,
     broken down by the year each market closed;
  2. how close to each market's close the last returned point lands;
  3. whether markets that are still open return finer data than resolved ones.

A public report (py-clob-client issue 216, 2025-12-22) says resolved markets return
nothing below 12-hour granularity. That report predates Polymarket's 2026 API
revision, so it is tested here rather than assumed.

The probe never writes any store. It prints a summary table and the full JSON.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FIDELITIES = (1, 60, 360, 720, 1440)  # minutes: 1m, 1h, 6h, 12h, 1d
WINDOW_FIDELITIES = (60, 720)
WINDOW_BEFORE_SECONDS = 14 * 86400
WINDOW_AFTER_SECONDS = 86400
YEARS = range(2021, 2027)
PER_YEAR = 6
KNOWN_SAMPLE = 16
OPEN_SAMPLE = 5
REQUEST_SPACING_SECONDS = 0.25
KNOWN_IDS = Path("docs/benchmarks/2026-09-12-metadata-probe.json")

GetJson = Callable[[str, dict[str, Any]], tuple[int | None, Any, str | None]]


# ── Pure helpers (unit-tested) ───────────────────────────────────────────────

def parse_token_ids(raw: Any) -> list[str]:
    """Gamma returns clobTokenIds as a JSON-encoded string list, sometimes a list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [str(token) for token in raw if str(token).strip()]


def parse_iso(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def close_timestamp(market: dict[str, Any]) -> tuple[int | None, str]:
    """Prefer the actual close time; fall back to the scheduled end date."""
    for field in ("closedTime", "endDate", "endDateIso"):
        ts = parse_iso(market.get(field))
        if ts is not None:
            return ts, field
    return None, ""


def series_points(payload: Any) -> list[tuple[int, float]]:
    """Normalize ``{"history": [{"t":..,"p":..}]}`` (or a bare list) to sorted points."""
    rows = payload.get("history") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    points: list[tuple[int, float]] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("t"), (int, float)):
            try:
                points.append((int(row["t"]), float(row["p"])))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(points)


def summarize_series(points: list[tuple[int, float]], close_ts: int | None) -> dict[str, Any]:
    if not points:
        return {"points": 0}
    spacing = [b[0] - a[0] for a, b in pairwise(points) if b[0] > a[0]]
    before = [p for p in points if close_ts is None or p[0] <= close_ts]
    last_before = before[-1] if before else None
    return {
        "points": len(points),
        "first_t": points[0][0],
        "last_t": points[-1][0],
        "median_spacing_minutes": round(statistics.median(spacing) / 60, 1) if spacing else None,
        "last_before_close_t": last_before[0] if last_before else None,
        "last_before_close_price": last_before[1] if last_before else None,
        "hours_last_point_before_close": (
            round((close_ts - last_before[0]) / 3600, 2)
            if last_before and close_ts is not None else None
        ),
    }


def aggregate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (group, request kind): coverage share and closing-gap medians."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for market in results:
        for kind, summary in market["requests"].items():
            groups[(market["group"], kind)].append(summary)
    rows = []
    for (group, kind), summaries in sorted(groups.items()):
        with_data = [s for s in summaries if s.get("points", 0) > 0]
        gaps = [s["hours_last_point_before_close"] for s in with_data
                if s.get("hours_last_point_before_close") is not None]
        spacing = [s["median_spacing_minutes"] for s in with_data
                   if s.get("median_spacing_minutes") is not None]
        rows.append({
            "group": group, "request": kind, "markets": len(summaries),
            "with_data": len(with_data),
            "share_with_data": round(len(with_data) / len(summaries), 3) if summaries else 0.0,
            "median_hours_last_point_before_close": (
                round(statistics.median(gaps), 2) if gaps else None
            ),
            "median_spacing_minutes": round(statistics.median(spacing), 1) if spacing else None,
        })
    return rows


# ── Network ──────────────────────────────────────────────────────────────────

def http_get_json(client: httpx.Client) -> GetJson:
    def get(url: str, params: dict[str, Any]) -> tuple[int | None, Any, str | None]:
        time.sleep(REQUEST_SPACING_SECONDS)
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            return None, None, f"{type(exc).__name__}: {exc}"
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body, None if response.is_success else response.text[:200]
    return get


def _market_record(market: dict[str, Any], group: str) -> dict[str, Any] | None:
    tokens = parse_token_ids(market.get("clobTokenIds"))
    close_ts, close_field = close_timestamp(market)
    if not tokens:
        return None
    return {
        "group": group, "condition_id": market.get("conditionId"),
        "question": str(market.get("question", ""))[:120], "token_id": tokens[0],
        "closed": market.get("closed"), "close_ts": close_ts, "close_field": close_field,
        "volume": market.get("volumeNum"), "requests": {},
    }


def sample_markets(get: GetJson, known_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    notes: dict[str, Any] = {"date_filter": {}}
    markets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(rows: Any, group_for: Callable[[dict[str, Any]], str | None], cap: int) -> None:
        added = 0
        for row in rows if isinstance(rows, list) else []:
            if added >= cap or not isinstance(row, dict):
                continue
            cid = str(row.get("conditionId", "")).lower()
            group = group_for(row)
            if not cid or cid in seen or group is None:
                continue
            record = _market_record(row, group)
            if record:
                markets.append(record)
                seen.add(cid)
                added += 1

    def by_close_year(row: dict[str, Any]) -> str | None:
        ts, _ = close_timestamp(row)
        return f"resolved-{datetime.fromtimestamp(ts, UTC).year}" if ts and row.get("closed") else None

    # Known real resolved markets from the committed 2026-09-12 metadata probe.
    for closed in ("true", "false"):
        _, rows, _ = get(f"{GAMMA}/markets", {
            "condition_ids": known_ids[:KNOWN_SAMPLE * 2], "closed": closed, "limit": 100,
        })
        add(rows, by_close_year, KNOWN_SAMPLE)

    # Spread across history: top-volume resolved markets per close year. The date
    # filter is not used elsewhere in this repo, so whether it was honoured is recorded.
    for year in YEARS:
        status, rows, error = get(f"{GAMMA}/markets", {
            "closed": "true", "limit": 50, "order": "volumeNum", "ascending": "false",
            "end_date_min": f"{year}-01-01T00:00:00Z", "end_date_max": f"{year}-12-31T23:59:59Z",
        })
        in_year = [r for r in rows if isinstance(r, dict) and (ts := close_timestamp(r)[0])
                   and datetime.fromtimestamp(ts, UTC).year == year] if isinstance(rows, list) else []
        notes["date_filter"][str(year)] = {
            "status": status, "rows": len(rows) if isinstance(rows, list) else 0,
            "rows_in_year": len(in_year), "error": error,
        }
        add(in_year, by_close_year, PER_YEAR)

    # Still-open markets, for comparison with resolved ones.
    _, rows, _ = get(f"{GAMMA}/markets", {
        "closed": "false", "active": "true", "limit": 20, "order": "volumeNum", "ascending": "false",
    })
    add(rows, lambda row: "open", OPEN_SAMPLE)
    return markets, notes


def probe_market(get: GetJson, market: dict[str, Any]) -> None:
    token, close_ts = market["token_id"], market["close_ts"]
    for fidelity in FIDELITIES:
        status, body, error = get(f"{CLOB}/prices-history",
                                  {"market": token, "interval": "max", "fidelity": fidelity})
        market["requests"][f"max@{fidelity}m"] = {
            "status": status, "error": error, **summarize_series(series_points(body), close_ts),
        }
    if close_ts is None or market["group"] == "open":
        return
    for fidelity in WINDOW_FIDELITIES:
        status, body, error = get(f"{CLOB}/prices-history", {
            "market": token, "startTs": close_ts - WINDOW_BEFORE_SECONDS,
            "endTs": close_ts + WINDOW_AFTER_SECONDS, "fidelity": fidelity,
        })
        market["requests"][f"window14d@{fidelity}m"] = {
            "status": status, "error": error, **summarize_series(series_points(body), close_ts),
        }


def render(summary: list[dict[str, Any]]) -> str:
    lines = [
        ("| Group | Request | Markets | With data | Share | Median h last point→close "
         "| Median spacing (min) |"),
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['group']} | {row['request']} | {row['markets']} | {row['with_data']} "
            f"| {row['share_with_data']:.0%} | {row['median_hours_last_point_before_close']} "
            f"| {row['median_spacing_minutes']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    known = json.loads(KNOWN_IDS.read_text(encoding="utf-8")).get("condition_ids", [])
    started = datetime.now(UTC).isoformat()
    headers = {"User-Agent": "MarketSignalOS-price-history-probe/0.1", "Accept": "application/json"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        get = http_get_json(client)
        markets, notes = sample_markets(get, [str(c) for c in known])
        for market in markets:
            probe_market(get, market)
    summary = aggregate(markets)
    report = {
        "schema_version": 1, "started_at": started, "finished_at": datetime.now(UTC).isoformat(),
        "endpoint": f"{CLOB}/prices-history", "fidelities_minutes": list(FIDELITIES),
        "sample_notes": notes, "markets_probed": len(markets), "summary": summary,
        "markets": markets,
    }
    sys.stdout.write(render(summary) + "\n\n")
    sys.stdout.write("PROBE_JSON_BEGIN\n" + json.dumps(report) + "\nPROBE_JSON_END\n")
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if markets else 1


if __name__ == "__main__":
    raise SystemExit(main())
