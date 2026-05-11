"""
Polymarket ingestor CLI.

Usage:
    marketsignalos-polymarket-ingestor leaderboard --metric profit --limit 50
    marketsignalos-polymarket-ingestor wallets --watchlist path/to/wallets.txt
    marketsignalos-polymarket-ingestor markets --closed
    marketsignalos-polymarket-ingestor all

Env vars:
    POLYMARKET_DATA_DIR          override the default services/ingestor/data
    POLYMARKET_WATCHLIST_PATH    default wallet watchlist (one address per line)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketWalletValue,
)
from .polymarket_client import PolymarketClient, PolymarketClientConfig
from .storage import (
    JsonlActivityStore,
    JsonlLeaderboardStore,
    JsonlMarketStore,
    JsonlPositionStore,
    JsonlWalletValueStore,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("marketsignalos.polymarket")


def _repo_root() -> Path:
    # services/polymarket-ingestor/src/marketsignalos_polymarket/runner.py
    return Path(__file__).resolve().parents[4]


def _data_dir() -> Path:
    configured = os.getenv("POLYMARKET_DATA_DIR")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data"


def _watchlist_path() -> Path:
    configured = os.getenv("POLYMARKET_WATCHLIST_PATH")
    if configured:
        return Path(configured)
    return _data_dir() / "polymarket_wallet_watchlist.txt"


# ── Parsing raw API dicts into typed dataclasses ─────────────────────────────

def _as_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    return float(str(val))


def _as_float_or_none(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except ValueError:
        return None


def _decode_string_list(val: Any) -> list[str]:
    """Gamma returns outcomes as a JSON-encoded string, not a list."""
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str) and val.startswith("["):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
    return []


def _decode_float_list(val: Any) -> list[float]:
    if isinstance(val, list):
        return [_as_float(v) for v in val]
    if isinstance(val, str) and val.startswith("["):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [_as_float(v) for v in parsed]
        except json.JSONDecodeError:
            pass
    return []


def parse_leaderboard_row(
    row: dict[str, Any], *, metric: str, window: str
) -> PolymarketLeaderboardEntry:
    return PolymarketLeaderboardEntry(
        proxy_wallet=str(row.get("proxyWallet", "")).lower(),
        name=str(row.get("name", "")),
        pseudonym=str(row.get("pseudonym", "")),
        amount_usdc=_as_float(row.get("amount")),
        metric=metric,
        window=window,
    )


def parse_activity_row(row: dict[str, Any]) -> PolymarketActivity:
    return PolymarketActivity(
        proxy_wallet=str(row.get("proxyWallet", "")).lower(),
        timestamp=int(row.get("timestamp", 0) or 0),
        condition_id=str(row.get("conditionId", "")),
        type=str(row.get("type", "")),
        side=str(row.get("side", "") or ""),
        size=_as_float(row.get("size")),
        usdc_size=_as_float(row.get("usdcSize")),
        price=_as_float(row.get("price")),
        outcome_index=int(row.get("outcomeIndex", 0) or 0),
        outcome=str(row.get("outcome", "") or ""),
        slug=str(row.get("slug", "")),
        title=str(row.get("title", "")),
        event_slug=str(row.get("eventSlug", "")),
        transaction_hash=str(row.get("transactionHash", "")),
        name=str(row.get("name", "")),
        pseudonym=str(row.get("pseudonym", "")),
    )


def parse_position_row(row: dict[str, Any], *, proxy_wallet: str) -> PolymarketPosition:
    return PolymarketPosition(
        proxy_wallet=proxy_wallet.lower(),
        condition_id=str(row.get("conditionId", "")),
        outcome_index=int(row.get("outcomeIndex", 0) or 0),
        outcome=str(row.get("outcome", "") or ""),
        size=_as_float(row.get("size")),
        avg_price=_as_float(row.get("avgPrice") or row.get("averagePrice")),
        current_value_usdc=_as_float(row.get("currentValue") or row.get("value")),
        slug=str(row.get("slug", "")),
        title=str(row.get("title", "")),
        event_slug=str(row.get("eventSlug", "")),
    )


def parse_market_row(row: dict[str, Any]) -> PolymarketMarket:
    return PolymarketMarket(
        gamma_id=str(row.get("id", "")),
        condition_id=str(row.get("conditionId", "")),
        slug=str(row.get("slug", "")),
        question=str(row.get("question", "")),
        category=str(row.get("category", "") or ""),
        end_date=str(row.get("endDate", "") or row.get("endDateIso", "")),
        outcomes=_decode_string_list(row.get("outcomes")),
        outcome_prices=_decode_float_list(row.get("outcomePrices")),
        volume_usdc=_as_float(row.get("volumeNum") or row.get("volume")),
        liquidity_usdc=_as_float(row.get("liquidityNum") or row.get("liquidity")),
        closed=bool(row.get("closed", False)),
        active=bool(row.get("active", False)),
        last_trade_price=_as_float_or_none(row.get("lastTradePrice")),
        best_bid=_as_float_or_none(row.get("bestBid")),
        best_ask=_as_float_or_none(row.get("bestAsk")),
    )


# ── Mode implementations ─────────────────────────────────────────────────────

@dataclass(slots=True)
class _Stores:
    leaderboard: JsonlLeaderboardStore
    activity: JsonlActivityStore
    positions: JsonlPositionStore
    markets: JsonlMarketStore
    values: JsonlWalletValueStore


def _build_stores(data_dir: Path) -> _Stores:
    data_dir.mkdir(parents=True, exist_ok=True)
    return _Stores(
        leaderboard=JsonlLeaderboardStore(data_dir / "polymarket_leaderboard.jsonl"),
        activity=JsonlActivityStore(data_dir / "polymarket_activity.jsonl"),
        positions=JsonlPositionStore(data_dir / "polymarket_positions.jsonl"),
        markets=JsonlMarketStore(data_dir / "polymarket_markets.jsonl"),
        values=JsonlWalletValueStore(data_dir / "polymarket_wallet_values.jsonl"),
    )


def _load_watchlist(path: Path) -> list[str]:
    if not path.exists():
        log.warning("watchlist not found path=%s", path)
        return []
    addresses: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            addresses.append(line.lower())
    return addresses


def run_leaderboard(
    client: PolymarketClient, stores: _Stores, *, metric: str, window: str, limit: int
) -> int:
    raw = client.get_leaderboard(metric=metric, window=window, limit=limit)
    entries = [parse_leaderboard_row(r, metric=metric, window=window) for r in raw]
    written = stores.leaderboard.write_leaderboard(entries)
    log.info("leaderboard metric=%s window=%s fetched=%d written=%d", metric, window, len(raw), written)
    return written


def run_wallets(
    client: PolymarketClient,
    stores: _Stores,
    *,
    addresses: list[str],
    activity_limit: int,
) -> tuple[int, int, int]:
    """Returns (activity_written, positions_written, values_written)."""
    total_activity = total_positions = total_values = 0
    for addr in addresses:
        try:
            raw_activity = client.get_wallet_activity(addr, limit=activity_limit)
            parsed_activity = [parse_activity_row(r) for r in raw_activity]
            total_activity += stores.activity.write_activity(parsed_activity)

            raw_positions = client.get_wallet_positions(addr)
            parsed_positions = [parse_position_row(r, proxy_wallet=addr) for r in raw_positions]
            total_positions += stores.positions.write_positions(parsed_positions)

            raw_value = client.get_wallet_value(addr)
            value = PolymarketWalletValue(
                proxy_wallet=addr,
                value_usdc=_as_float(raw_value.get("value")),
            )
            total_values += stores.values.write_values([value])

            log.info(
                "wallet wallet=%s activity_new=%d positions=%d value_usdc=%.2f",
                addr,
                len(parsed_activity),
                len(parsed_positions),
                value.value_usdc,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("wallet_failed wallet=%s error=%s", addr, exc)
    return total_activity, total_positions, total_values


def run_markets(
    client: PolymarketClient,
    stores: _Stores,
    *,
    closed: bool | None,
    pages: int,
    page_size: int,
) -> int:
    total = 0
    for page in range(pages):
        raw = client.get_markets(closed=closed, limit=page_size, offset=page * page_size)
        if not raw:
            break
        parsed = [parse_market_row(r) for r in raw]
        total += stores.markets.write_markets(parsed)
        log.info("markets page=%d fetched=%d total_new=%d", page, len(raw), total)
        if len(raw) < page_size:
            break
    return total


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketsignalos-polymarket-ingestor")
    sub = parser.add_subparsers(dest="mode", required=True)

    lb = sub.add_parser("leaderboard", help="Fetch the public Polymarket leaderboard")
    lb.add_argument("--metric", choices=["profit", "volume"], default="profit")
    lb.add_argument("--window", default="all")
    lb.add_argument("--limit", type=int, default=50)

    wa = sub.add_parser("wallets", help="Pull activity + positions + value for a wallet list")
    wa.add_argument("--watchlist", type=Path, default=None)
    wa.add_argument("--address", action="append", default=[], help="One-off address (repeatable)")
    wa.add_argument("--activity-limit", type=int, default=500)

    mk = sub.add_parser("markets", help="Fetch market metadata from Gamma")
    mk.add_argument("--closed", action="store_true", help="Fetch resolved markets")
    mk.add_argument("--active", action="store_true", help="Fetch only active markets")
    mk.add_argument("--pages", type=int, default=5)
    mk.add_argument("--page-size", type=int, default=100)

    sub.add_parser("all", help="Run leaderboard + watchlist wallets + markets in one pass")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stores = _build_stores(_data_dir())
    client = PolymarketClient(PolymarketClientConfig.from_env())

    try:
        if args.mode == "leaderboard":
            run_leaderboard(client, stores, metric=args.metric, window=args.window, limit=args.limit)
        elif args.mode == "wallets":
            addresses = list(args.address)
            wl_path = args.watchlist or _watchlist_path()
            addresses.extend(_load_watchlist(wl_path))
            addresses = sorted(set(a for a in addresses if a))
            if not addresses:
                log.error("no wallets to ingest — pass --address or populate %s", wl_path)
                return 1
            run_wallets(client, stores, addresses=addresses, activity_limit=args.activity_limit)
        elif args.mode == "markets":
            closed: bool | None = None
            if args.closed:
                closed = True
            elif args.active:
                closed = False
            run_markets(client, stores, closed=closed, pages=args.pages, page_size=args.page_size)
        elif args.mode == "all":
            run_leaderboard(client, stores, metric="profit", window="all", limit=50)
            run_leaderboard(client, stores, metric="volume", window="all", limit=50)
            addresses = _load_watchlist(_watchlist_path())
            if addresses:
                run_wallets(client, stores, addresses=addresses, activity_limit=500)
            else:
                log.info("watchlist empty — skipping wallets pass")
            run_markets(client, stores, closed=None, pages=5, page_size=100)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
