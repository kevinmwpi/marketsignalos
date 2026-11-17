"""Live smoke test of the Data API v2 migration. Writes counts and booleans only.

Runs the migrated client and collection code against Polymarket's public API:

1. ``v1_parity``: the last research snapshot collected from v1 (2026-11-02) holds
   the 100 newest trades of ten wallets. The same wallets and window are read
   from v2 through ``PolymarketClient`` and compared fill by fill.
2. ``resume``: one long activity walk against the same range read as short walks
   that stop and resume, as the worker's page cap makes them.
3. ``wallet_endpoints``: positions, closed positions, value and economics for the
   wallet at the top of the month's PnL leaderboard.
4. ``pipeline``: two collection runs into an empty temporary directory with the
   worker's settings and a smaller wallet batch.
5. ``snapshot``: the research-snapshot collector, and whether its output would
   pass the publisher's checks.

No price, size, value or PnL of any wallet is written: only how many rows matched.

    uv run --no-sync python scripts/smoke_data_api_v2.py \\
        --v1-snapshot /path/to/research-snapshot.json \\
        --out docs/benchmarks/2026-11-17-data-api-v2-smoke.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
from marketsignalos_polymarket import runner
from marketsignalos_polymarket.models import PolymarketActivity
from marketsignalos_polymarket.polymarket_client import PolymarketClient

LOG = logging.getLogger("smoke_data_api_v2")
PAUSE = 0.25
Fill = tuple[str, str, str, str, int]  # tx, condition, token, side, timestamp


def _close(a: float | None, b: float | None, *, tolerance: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def _guard(name: str, part: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = part()
    except Exception as exc:  # a failed part must not hide the others
        LOG.exception("%s failed", name)
        result = {"error_type": type(exc).__name__, "error": str(exc)[:300]}
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


# ── 1. Parity with the last v1 snapshot ──────────────────────────────────────

def _v2_trades(
    client: PolymarketClient, address: str, start: int, end: int, *, max_pages: int = 30
) -> tuple[list[dict[str, Any]], bool]:
    """Every non-combo TRADE row of ``[start, end]``, and whether the walk finished."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(max_pages):
        page = client.get_wallet_activity_page(
            address, limit=1000, cursor=cursor, start=start, end=end,
        )
        time.sleep(PAUSE)
        rows.extend(
            row for row in page.rows
            if row.get("type") == "TRADE" and row.get("is_combo") is not True
        )
        cursor = page.next_cursor
        if cursor is None:
            return rows, True
    return rows, False


def v1_parity(client: PolymarketClient, snapshot_path: Path) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    window_end = int(snapshot["trade_window"]["end"])
    totals: Counter[str] = Counter()
    wallets: list[dict[str, Any]] = []
    for wallet in snapshot["wallets"]:
        v1_rows = wallet["trades"]["rows"]
        if not v1_rows:
            continue
        oldest = min(int(row["timestamp"]) for row in v1_rows)
        # A full v1 page stops inside its oldest second: compare above it.
        floor = oldest + 1 if wallet["trades"]["possibly_truncated"] else oldest
        v2_rows, finished = _v2_trades(client, wallet["address"], floor, window_end)

        def key_v1(row: dict[str, Any]) -> Fill:
            return (str(row["transaction_hash"]).lower(), row["condition_id"], row["asset"],
                    row["side"], int(row["timestamp"]))

        def key_v2(row: dict[str, Any]) -> Fill:
            return (str(row.get("transaction_hash", "")).lower(),
                    str(row.get("condition_id", "")), str(row.get("token_id", "")),
                    str(row.get("side", "")), int(row.get("timestamp", 0)))

        v1 = [row for row in v1_rows if int(row["timestamp"]) >= floor]
        v1_keys = Counter(key_v1(row) for row in v1)
        v2_keys = Counter(key_v2(row) for row in v2_rows)
        counts: Counter[str] = Counter(
            v1_rows=len(v1), v2_rows=len(v2_rows),
            same_fill_count=sum((v1_keys & v2_keys).values()),
            v1_only=sum((v1_keys - v2_keys).values()),
            v2_only=sum((v2_keys - v1_keys).values()),
            v1_rows_sharing_a_fill_key=len(v1) - len(v1_keys),
            v2_rows_sharing_a_fill_key=len(v2_rows) - len(v2_keys),
        )
        # Values, for the keys both sides serve exactly once.
        v2_by_key = {key_v2(row): row for row in v2_rows}
        for row in v1:
            key = key_v1(row)
            if v1_keys[key] != 1 or v2_keys[key] != 1:
                continue
            other = v2_by_key[key]
            counts["single_fills_compared"] += 1
            counts["size_equal"] += _close(row["size"], other.get("size"))
            counts["price_equal"] += _close(row["price"], other.get("price"))
            counts["notional_equal"] += _close(row["notional"], other.get("usdc_size"))
            counts["outcome_equal"] += row["outcome"] == other.get("outcome")
        # Totals, for the keys served more than once on either side.
        for key in (v1_keys | v2_keys):
            if v1_keys[key] <= 1 and v2_keys[key] <= 1:
                continue
            counts["multi_fill_keys"] += 1
            v1_size = sum(row["size"] for row in v1 if key_v1(row) == key)
            v2_size = sum(float(row.get("size", 0)) for row in v2_rows if key_v2(row) == key)
            counts["multi_fill_same_row_count"] += v1_keys[key] == v2_keys[key]
            counts["multi_fill_same_total_size"] += _close(v1_size, v2_size)
        totals.update(counts)
        wallets.append({"finished": finished, **dict(counts)})
    return {
        "v1_snapshot_generated_at": snapshot["generated_at"],
        "wallets_compared": len(wallets),
        "totals": dict(totals),
        "per_wallet": wallets,
    }


# ── 2. Stopped and resumed walks ─────────────────────────────────────────────

def _identity(event: PolymarketActivity) -> tuple[Any, ...]:
    row = asdict(event)
    row.pop("fetched_at")
    return tuple(row.values())


def resume(client: PolymarketClient, address: str) -> dict[str, Any]:
    end = int(time.time()) - 300  # both readings see the same, settled range

    def walk(page_size: int, max_pages: int, end_timestamp: int) -> tuple[
        list[PolymarketActivity], runner._ActivityPaginationResult
    ]:
        events: list[PolymarketActivity] = []
        result = runner._paginate_activity_window(
            client, address, page_size=page_size, max_pages=max_pages,
            since_timestamp=None, end_timestamp=end_timestamp, on_page=events.extend,
        )
        time.sleep(PAUSE)
        return events, result

    reference, ref_result = walk(500, 4, end)
    if ref_result.oldest_timestamp is None:
        return {"reference_rows": 0}
    target = ref_result.oldest_timestamp

    resumed: list[PolymarketActivity] = []
    walks = stalled = 0
    errors: list[str] = []
    cursor: int | None = end
    exhausted = False
    while cursor is not None and cursor >= target and walks < 60:
        events, result = walk(100, 2, cursor)
        walks += 1
        resumed.extend(events)
        stalled += result.stalled
        if result.exhausted:
            exhausted = True
            break
        cursor = runner._resume_end(result, errors)

    # Above the second the reference stopped in, both readings must hold the same
    # rows. A resumed walk re-reads the second it stopped in, so it may hold more
    # copies of a row, never fewer.
    ref_rows = Counter(_identity(e) for e in reference if e.timestamp > target)
    resumed_rows = Counter(_identity(e) for e in resumed if e.timestamp > target)
    store_keys = {runner._activity_key(e) for e in reference if e.timestamp > target}
    return {
        "reference_rows": sum(ref_rows.values()),
        "reference_distinct_rows": len(ref_rows),
        "reference_exhausted": ref_result.exhausted,
        "resumed_walks": walks,
        "resumed_rows_read": sum(resumed_rows.values()),
        "resumed_distinct_rows": len(resumed_rows),
        "rows_missing_from_resumed": len(set(ref_rows) - set(resumed_rows)),
        "rows_only_in_resumed": len(set(resumed_rows) - set(ref_rows)),
        "rows_with_fewer_copies_in_resumed": sum(
            resumed_rows[row] < copies for row, copies in ref_rows.items()
        ),
        "resumed_exhausted": exhausted,
        "stalled_walks": stalled,
        "errors": errors,
        "distinct_store_keys": len(store_keys),
        "rows_the_store_key_collapses": sum(ref_rows.values()) - len(store_keys),
    }


# ── 3. Wallet endpoints ──────────────────────────────────────────────────────

def wallet_endpoints(client: PolymarketClient, address: str) -> dict[str, Any]:
    out: dict[str, Any] = {}

    raw_pages: list[int] = []
    served: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(40):
        page = client.get_wallet_positions_page(
            address, limit=500, cursor=cursor,
            min_shares=runner._MIN_POSITION_SHARES, sort_by="TOKENS",
        )
        time.sleep(PAUSE)
        raw_pages.append(len(page.rows))
        served.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break
    sizes = [float(row.get("current_size") or 0) for row in served]
    keys = [(row.get("condition_id"), row.get("outcome_index")) for row in served]

    def near(row: dict[str, Any], basis: float) -> bool:
        expected = float(row["current_value"]) - basis
        return abs(float(row["unrealized_pnl"]) - expected) <= max(
            0.02, 1e-3 * abs(float(row["current_value"]))
        )

    numeric = [
        row for row in served
        if all(isinstance(row.get(name), int | float) for name in (
            "current_size", "avg_price", "current_value", "unrealized_pnl", "entry_cost_usdc"))
    ]

    out["positions"] = {
        "pages": raw_pages,
        "walk_finished": cursor is None,
        "rows": len(served),
        "rows_served_twice": len(keys) - len(set(keys)),
        "below_one_share": sum(size < 1 for size in sizes),
        "sizes_non_increasing": all(a >= b for a, b in pairwise(sizes)),
        "statuses": dict(Counter(str(row.get("status")) for row in served)),
        "redeemable_rows": sum(row.get("redeemable") is True for row in served),
        "null_current_size": sum(row.get("current_size") is None for row in served),
        "rows_with_every_number": len(numeric),
        "unrealized_is_value_minus_size_times_avg_price": sum(
            near(row, float(row["current_size"]) * float(row["avg_price"])) for row in numeric
        ),
        "unrealized_is_value_minus_entry_cost": sum(
            near(row, float(row["entry_cost_usdc"])) for row in numeric
        ),
        "parsed": len([
            runner.parse_position_row(row, proxy_wallet=address) for row in served
        ]),
    }
    # The same listing through the runner, against the default (value) ordering.
    by_size = {
        (str(row.get("condition_id")), str(row.get("outcome_index")))
        for row in runner._paginate_positions(client, address)
    }
    by_value: set[tuple[str, str]] = set()
    cursor = None
    for _page in range(40):
        page = client.get_wallet_positions_page(
            address, limit=500, cursor=cursor, min_shares=runner._MIN_POSITION_SHARES,
        )
        time.sleep(PAUSE)
        by_value.update(
            (str(row.get("condition_id")), str(row.get("outcome_index"))) for row in page.rows
        )
        cursor = page.next_cursor
        if cursor is None:
            break
    out["positions_ordering"] = {
        "by_size": len(by_size), "by_value": len(by_value),
        "only_by_size": len(by_size - by_value), "only_by_value": len(by_value - by_size),
    }

    closed = runner._fetch_closed_positions(client, address)
    realized = [float(row.get("realized_pnl") or 0) for row in closed]
    out["closed_positions"] = {
        "rows": len(closed),
        "statuses": dict(Counter(str(row.get("status")) for row in closed)),
        "realized_pnl_non_increasing": all(
            a >= b for a, b in pairwise(realized)
        ),
        "parsed": len([
            runner.parse_closed_position_row(row, proxy_wallet=address) for row in closed
        ]),
    }

    value = client.get_wallet_value(address)
    out["value"] = {"keys": sorted(value), "value_is_number": isinstance(
        value.get("value"), int | float)}
    for period in ("ALL", "MONTH"):
        standing = client.get_wallet_leaderboard_standing(address, time_period=period)
        out[f"standing_{period.lower()}"] = {
            "present": bool(standing),
            "pnl_is_number": isinstance(standing.get("pnl"), int | float),
        }
    volume = client.get_wallet_volume(address)
    out["volume"] = {"keys": sorted(volume), "volume_usdc_is_number": isinstance(
        volume.get("volume_usdc"), int | float)}
    return out


# ── 4. The collection pipeline, twice ────────────────────────────────────────

def pipeline(wallets: int) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="v2-smoke-") as directory:
        os.environ["POLYMARKET_DATA_DIR"] = directory
        os.environ.pop("POLYMARKET_WATCHLIST_PATH", None)
        os.environ.pop("DATABASE_URL", None)
        data_dir = Path(directory)
        for _run in range(2):
            started = time.monotonic()
            result = runner.run_pipeline(  # the lean pilot's call, with a smaller batch
                windows=["month"], leaderboard_limit=wallets, seed_metrics=("profit",),
                wallet_batch_size=wallets, skip_enrichment=True, max_pages_per_wallet=2,
                market_pages=1, refresh_reference=False,
                max_activity_requests_per_wallet=10, max_watchlist=wallets,
            ).to_dict()
            stores = runner._build_stores(data_dir)
            hydration = list(stores.hydration.load_hydration().values())
            activity = list(runner._iter_jsonl(stores.activity_path))
            store_keys = Counter(
                (row.get("transaction_hash"), row.get("condition_id"),
                 row.get("outcome_index"), row.get("type"))
                for row in activity
            )
            gaps = data_dir / "polymarket_activity_gaps.json"
            runs.append({
                "seconds": round(time.monotonic() - started, 1),
                "result": {
                    key: value for key, value in result.items()
                    if isinstance(value, int | bool | list) or value is None
                },
                "warning": result.get("warning"),
                "hydration_wallets": len(hydration),
                "activity_history_complete": sum(h.activity_history_complete for h in hydration),
                "positions_complete": sum(h.positions_complete for h in hydration),
                "closed_positions_complete": sum(h.closed_positions_complete for h in hydration),
                "economic_all_time_complete": sum(
                    h.economic_all_time_complete for h in hydration),
                "economic_month_complete": sum(h.economic_month_complete for h in hydration),
                "all_time_volume_positive": sum(h.all_time_volume_usdc > 0 for h in hydration),
                "history_cursor_set": sum(
                    h.oldest_activity_cursor_timestamp is not None for h in hydration),
                "error_kinds": runner._error_kinds([h.errors for h in hydration]),
                "activity_rows": len(activity),
                "activity_types": dict(Counter(str(row.get("type")) for row in activity)),
                "activity_rows_sharing_a_store_key": len(activity) - len(store_keys),
                "activity_outcome_index_999": sum(
                    row.get("outcome_index") == 999 for row in activity),
                "trades_without_condition": sum(
                    row.get("type") == "TRADE" and not row.get("condition_id")
                    for row in activity),
                "gaps_file_present": gaps.exists(),
                "position_rows": sum(1 for _ in runner._iter_jsonl(
                    data_dir / "polymarket_positions.jsonl")),
                "closed_position_rows": sum(1 for _ in runner._iter_jsonl(
                    data_dir / "polymarket_closed_positions.jsonl")),
            })
    return {"runs": runs}


# ── 5. The research-snapshot collector ───────────────────────────────────────

def snapshot(wallets: int) -> dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    collector: Any = importlib.import_module("collect_research_snapshot")
    publisher: Any = importlib.import_module("publish_research_snapshot")
    with httpx.Client(
        follow_redirects=False,
        headers={"User-Agent": "MarketSignalOS/research-snapshot-v1"},
    ) as http:
        captured = collector.Collector(http, wallets, 120).collect()
    out: dict[str, Any] = {
        "status": captured["status"],
        "wallets": len(captured["wallets"]),
        "requests": [
            {key: request.get(key) for key in (
                "path", "status", "http_status", "rows_returned", "rows_rejected", "error")}
            for request in captured["requests"]
        ],
        "samples": [
            {kind: {key: wallet[kind][key] for key in (
                "status", "rows_returned", "rows_rejected", "rows_excluded",
                "possibly_truncated")} | {"rows": len(wallet[kind]["rows"])}
             for kind in ("trades", "positions")}
            for wallet in captured["wallets"]
        ],
    }
    with tempfile.TemporaryDirectory(prefix="v2-smoke-") as directory:
        path = Path(directory) / "research-snapshot.json"
        collector.publish(captured, path)
        try:
            publisher.publication_bytes(path, "0" * 40, "smoke")
            out["publishable"] = True
        except ValueError as exc:
            out["publishable"] = False
            out["publication_refused"] = str(exc)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--v1-snapshot", type=Path, required=True,
                        help="research-snapshot.json from the codex/research-snapshots branch")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wallets", type=int, default=4,
                        help="wallets for the pipeline and snapshot parts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
    }
    client = PolymarketClient()
    active: list[str] = []

    def leaderboard() -> dict[str, Any]:
        top = client.get_trader_leaderboard_page(time_period="MONTH", order_by="PNL", limit=5)
        active.append(str(top.rows[0]["user_id"]))
        seed = runner._fetch_seed_leaderboard(
            client, metric="profit", window="month", limit=120)
        return {
            "rows": len(top.rows),
            "keys": sorted(top.rows[0]),
            "seed_entries": len(seed),  # 120 asked for, in pages of 50
            "seed_distinct_wallets": len({entry.proxy_wallet for entry in seed}),
            "seed_names_present": sum(bool(entry.name) for entry in seed),
        }

    try:
        report["leaderboard"] = _guard("leaderboard", leaderboard)
        report["v1_parity"] = _guard("v1_parity", lambda: v1_parity(client, args.v1_snapshot))
        if active:
            report["resume"] = _guard("resume", lambda: resume(client, active[0]))
            report["wallet_endpoints"] = _guard(
                "wallet_endpoints", lambda: wallet_endpoints(client, active[0]))
    finally:
        client.close()
    report["pipeline"] = _guard("pipeline", lambda: pipeline(args.wallets))
    report["snapshot"] = _guard("snapshot", lambda: snapshot(args.wallets))
    report["finished_at"] = datetime.now(UTC).isoformat()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = [name for name, part in report.items()
              if isinstance(part, dict) and "error_type" in part]
    sys.stdout.write(f"wrote {args.out}; parts that raised: {failed or 'none'}\n")


if __name__ == "__main__":
    main()
