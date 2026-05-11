r"""
Phase 0 discovery probe -- Polymarket public APIs.

Hits candidate endpoints across Gamma, CLOB, Data, leaderboard, and Goldsky,
reports which return JSON, and dumps top-level field names so we know the
real shape before scaffolding the ingestor.

Usage:
    .\.venv\Scripts\python.exe scripts\probe_polymarket.py
    # or to also probe a specific wallet:
    .\.venv\Scripts\python.exe scripts\probe_polymarket.py 0xabc...

Writes findings to docs/polymarket-api-discovery.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / "docs" / "polymarket-api-discovery.md"

TIMEOUT = httpx.Timeout(15.0, connect=10.0)
HEADERS = {
    "User-Agent": "MarketSignalOS-discovery/0.1 (research)",
    "Accept": "application/json",
}

# Each probe: (label, method, url, optional json body)
# A wallet address is interpolated when provided.
BASE_PROBES: list[tuple[str, str, str, dict[str, Any] | None]] = [
    # ── Gamma (market metadata) ──
    ("gamma:markets", "GET", "https://gamma-api.polymarket.com/markets?limit=2&active=true&closed=false", None),
    ("gamma:events", "GET", "https://gamma-api.polymarket.com/events?limit=2&active=true&closed=false", None),
    ("gamma:markets_resolved", "GET", "https://gamma-api.polymarket.com/markets?limit=2&closed=true", None),

    # ── CLOB (orderbook + market prices) ──
    ("clob:markets", "GET", "https://clob.polymarket.com/markets?limit=2", None),
    ("clob:sampling-markets", "GET", "https://clob.polymarket.com/sampling-markets?limit=2", None),

    # ── Leaderboard candidates ──
    ("lb:profit_week", "GET", "https://lb-api.polymarket.com/profit?window=week&limit=10", None),
    ("lb:volume_week", "GET", "https://lb-api.polymarket.com/volume?window=week&limit=10", None),
    ("lb:profit_all", "GET", "https://lb-api.polymarket.com/profit?window=all&limit=10", None),
    ("data:leaderboards_profit", "GET", "https://data-api.polymarket.com/leaderboards/profit?window=week&limit=10", None),
    ("data:leaderboards_volume", "GET", "https://data-api.polymarket.com/leaderboards/volume?window=week&limit=10", None),

    # ── Data API (per-wallet) — only meaningful if WALLET is provided ──
    ("data:positions", "GET", "https://data-api.polymarket.com/positions?user={wallet}&limit=5", None),
    ("data:activity", "GET", "https://data-api.polymarket.com/activity?user={wallet}&limit=5", None),
    ("data:value", "GET", "https://data-api.polymarket.com/value?user={wallet}", None),
    ("data:trades", "GET", "https://data-api.polymarket.com/trades?user={wallet}&limit=5", None),

    # ── Goldsky subgraph (GraphQL) ──
    (
        "goldsky:meta",
        "POST",
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn",
        {"query": "{ _meta { block { number } deployment hasIndexingErrors } }"},
    ),
    (
        "goldsky:trades_sample",
        "POST",
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/polymarket-orderbook-resync/prod/gn",
        {
            "query": (
                "{ orderFilledEvents(first: 3, orderBy: timestamp, orderDirection: desc) "
                "{ id maker taker makerAssetId takerAssetId makerAmountFilled "
                "takerAmountFilled fee timestamp transactionHash } }"
            )
        },
    ),
]


def _shape_of(obj: Any, depth: int = 0, max_depth: int = 2) -> Any:
    """Return a compact description of a JSON value's shape."""
    if depth >= max_depth:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: _shape_of(v, depth + 1, max_depth) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        return [_shape_of(obj[0], depth + 1, max_depth)] if obj else []
    if isinstance(obj, str):
        return f"str({len(obj)})" if len(obj) > 40 else f"str:{obj[:40]!r}"
    return type(obj).__name__


def _probe_one(
    client: httpx.Client, label: str, method: str, url: str, body: dict[str, Any] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {"label": label, "method": method, "url": url}
    try:
        if method == "GET":
            resp = client.get(url)
        else:
            resp = client.post(url, json=body)
    except httpx.HTTPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["status"] = resp.status_code
    result["content_type"] = resp.headers.get("content-type", "")
    result["body_bytes"] = len(resp.content)

    if "json" in result["content_type"]:
        try:
            body_json = resp.json()
            result["shape"] = _shape_of(body_json, max_depth=3)
            if isinstance(body_json, list) and body_json:
                result["count"] = len(body_json)
                result["first_keys"] = list(body_json[0].keys()) if isinstance(body_json[0], dict) else None
            elif isinstance(body_json, dict):
                result["top_keys"] = list(body_json.keys())
        except ValueError:
            result["snippet"] = resp.text[:300]
    else:
        result["snippet"] = resp.text[:300]

    return result


def _write_report(results: list[dict[str, Any]], wallet: str | None) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Polymarket API discovery report\n")
    lines.append(f"Wallet probed: `{wallet or '(none provided)'}`\n")
    lines.append(f"Probes: {len(results)}\n")
    ok_count = sum(1 for r in results if r.get("status") == 200 and "json" in r.get("content_type", ""))
    lines.append(f"Returning 200 + JSON: {ok_count}/{len(results)}\n\n")

    for r in results:
        lines.append(f"## `{r['label']}`\n")
        if "skipped" in r:
            lines.append(f"- SKIPPED: {r['skipped']}")
            lines.append(f"- url template: `{r.get('url', '')}`")
            lines.append("")
            continue
        lines.append(f"- {r.get('method', '?')} `{r.get('url', '')}`")
        if "error" in r:
            lines.append(f"- ERROR: `{r['error']}`")
        else:
            lines.append(f"- status `{r.get('status')}` · content-type `{r.get('content_type', '')}` · bytes {r.get('body_bytes', 0)}")
            if "count" in r:
                lines.append(f"- list length: {r['count']}")
            if "first_keys" in r and r["first_keys"]:
                lines.append(f"- first row keys: `{r['first_keys']}`")
            if "top_keys" in r:
                lines.append(f"- top-level keys: `{r['top_keys']}`")
            if "shape" in r:
                lines.append(f"- shape:\n```json\n{json.dumps(r['shape'], indent=2)[:2000]}\n```")
            if "snippet" in r:
                lines.append(f"- snippet: `{r['snippet'][:200]}`")
        lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")


def main() -> int:
    wallet = sys.argv[1] if len(sys.argv) > 1 else None
    results: list[dict[str, Any]] = []

    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        for label, method, url, body in BASE_PROBES:
            if "{wallet}" in url:
                if not wallet:
                    print(f"[skip] {label} — needs wallet arg")
                    results.append({"label": label, "skipped": "no wallet provided", "url": url})
                    continue
                url = url.replace("{wallet}", wallet)
            print(f"[probe] {label} -> {method} {url[:80]}", flush=True)
            res = _probe_one(client, label, method, url, body)
            print(f"        status={res.get('status')} ct={res.get('content_type', '')[:30]}", flush=True)
            results.append(res)

    _write_report(results, wallet)

    # Print compact summary table
    print("\n--- SUMMARY ---")
    for r in results:
        status = r.get("status", "—")
        ct = (r.get("content_type") or "")[:20]
        extra = ""
        if "count" in r:
            extra = f" [{r['count']} items]"
        elif "top_keys" in r:
            extra = f" keys={r['top_keys'][:5]}"
        elif "skipped" in r:
            extra = f" SKIP({r['skipped']})"
        elif "error" in r:
            extra = f" ERR"
        print(f"  {r['label']:30s} {status!s:>5}  {ct:<20s}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
