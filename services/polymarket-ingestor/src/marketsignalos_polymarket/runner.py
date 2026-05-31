"""
Polymarket ingestor CLI.

Usage:
    marketsignalos-polymarket-ingestor leaderboard --metric profit --limit 50
    marketsignalos-polymarket-ingestor wallets --watchlist path/to/wallets.txt
    marketsignalos-polymarket-ingestor markets --closed
    marketsignalos-polymarket-ingestor all

Env vars:
    POLYMARKET_DATA_DIR          override the default data dir
                                 (./services/ingestor/data — preserved at
                                 that path for back-compat; gitignored)
    POLYMARKET_WATCHLIST_PATH    default wallet watchlist (one address per line)
    INGEST_POLYMARKET            kill switch for 'all' mode (set to "1" to enable);
                                 individual subcommands always run. This lets the
                                 ingestor be safely deployed alongside the API
                                 before any backfill load is desired.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .kalshi_markets_fetch import (
    KalshiMarket,
    fetch_kalshi_markets,
    load_kalshi_markets_jsonl,
    write_kalshi_markets_jsonl,
)
from .market_matcher import (
    MatchConfig,
    NormalizedMarket,
    match_markets,
)
from .models import (
    MarketLink,
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketWalletReviewState,
    PolymarketWalletValue,
)
from .polymarket_client import PolymarketClient, PolymarketClientConfig
from .skill_computation import compute_all_enrichment
from .storage import (
    ActivityStore,
    DualActivityStore,
    DualEnrichmentStore,
    DualLeaderboardStore,
    DualMarketLinkStore,
    DualMarketStore,
    DualPositionStore,
    DualWalletCheckpointStore,
    DualWalletValueStore,
    EnrichmentStore,
    JsonlActivityStore,
    JsonlEnrichmentStore,
    JsonlLeaderboardStore,
    JsonlMarketLinkStore,
    JsonlMarketStore,
    JsonlPositionStore,
    JsonlWalletValueStore,
    JsonWalletCheckpointStore,
    LeaderboardStore,
    MarketLinkStore,
    MarketStore,
    PositionStore,
    PostgresActivityStore,
    PostgresEnrichmentStore,
    PostgresLeaderboardStore,
    PostgresMarketLinkStore,
    PostgresMarketStore,
    PostgresPositionStore,
    PostgresWalletCheckpointStore,
    PostgresWalletValueStore,
    WalletCheckpointStore,
    WalletValueStore,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("marketsignalos.polymarket")

ProgressCallback = Callable[[dict[str, Any]], None]


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


def _review_state_path() -> Path:
    return _data_dir() / "polymarket_wallet_review_state.jsonl"


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
    leaderboard: LeaderboardStore
    activity: ActivityStore
    positions: PositionStore
    markets: MarketStore
    values: WalletValueStore
    checkpoints: WalletCheckpointStore
    enrichment: EnrichmentStore
    market_links: MarketLinkStore

    # Paths exposed so reading-only steps can re-read the JSONL files.
    activity_path: Path
    markets_path: Path
    leaderboard_path: Path
    kalshi_markets_path: Path


def _build_stores(data_dir: Path) -> _Stores:
    data_dir.mkdir(parents=True, exist_ok=True)
    activity_path = data_dir / "polymarket_activity.jsonl"
    markets_path = data_dir / "polymarket_markets.jsonl"
    leaderboard_path = data_dir / "polymarket_leaderboard.jsonl"
    kalshi_markets_path = data_dir / "kalshi_markets.jsonl"

    jsonl_leaderboard = JsonlLeaderboardStore(leaderboard_path)
    jsonl_activity = JsonlActivityStore(activity_path)
    jsonl_positions = JsonlPositionStore(data_dir / "polymarket_positions.jsonl")
    jsonl_markets = JsonlMarketStore(markets_path)
    jsonl_values = JsonlWalletValueStore(data_dir / "polymarket_wallet_values.jsonl")
    jsonl_checkpoints = JsonWalletCheckpointStore(
        data_dir / "polymarket_wallet_checkpoints.json"
    )
    jsonl_enrichment = JsonlEnrichmentStore(data_dir / "polymarket_wallet_enrichment.jsonl")
    jsonl_market_links = JsonlMarketLinkStore(data_dir / "market_links.jsonl")

    # When DATABASE_URL is set, fan every write out to Postgres as well. The
    # API continues reading from JSONL during Phase 7.5, so JSONL stays the
    # system of record — Dual wrappers report the JSONL write count and read
    # from JSONL.
    database_url = os.getenv("DATABASE_URL", "").strip()
    leaderboard: LeaderboardStore = jsonl_leaderboard
    activity: ActivityStore = jsonl_activity
    positions: PositionStore = jsonl_positions
    markets: MarketStore = jsonl_markets
    values: WalletValueStore = jsonl_values
    checkpoints: WalletCheckpointStore = jsonl_checkpoints
    enrichment: EnrichmentStore = jsonl_enrichment
    market_links: MarketLinkStore = jsonl_market_links
    if database_url:
        log.info("dual-write enabled (DATABASE_URL set)")
        leaderboard = DualLeaderboardStore(jsonl_leaderboard,
                                          PostgresLeaderboardStore(database_url))
        activity = DualActivityStore(jsonl_activity,
                                    PostgresActivityStore(database_url))
        positions = DualPositionStore(jsonl_positions,
                                     PostgresPositionStore(database_url))
        markets = DualMarketStore(jsonl_markets, PostgresMarketStore(database_url))
        values = DualWalletValueStore(jsonl_values,
                                     PostgresWalletValueStore(database_url))
        checkpoints = DualWalletCheckpointStore(jsonl_checkpoints,
                                               PostgresWalletCheckpointStore(database_url))
        enrichment = DualEnrichmentStore(jsonl_enrichment,
                                        PostgresEnrichmentStore(database_url))
        market_links = DualMarketLinkStore(jsonl_market_links,
                                          PostgresMarketLinkStore(database_url))

    return _Stores(
        leaderboard=leaderboard,
        activity=activity,
        positions=positions,
        markets=markets,
        values=values,
        checkpoints=checkpoints,
        enrichment=enrichment,
        market_links=market_links,
        activity_path=activity_path,
        markets_path=markets_path,
        leaderboard_path=leaderboard_path,
        kalshi_markets_path=kalshi_markets_path,
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


def _paginate_activity(
    client: PolymarketClient,
    address: str,
    *,
    page_size: int,
    max_pages: int,
    since_timestamp: int | None,
) -> list[PolymarketActivity]:
    """
    Walk /activity with offset pagination until we either exhaust the API,
    hit max_pages, or reach an event older than since_timestamp.

    Activity is returned newest-first, so we stop as soon as the oldest row
    in a page is <= since_timestamp.
    """
    collected: list[PolymarketActivity] = []
    for page in range(max_pages):
        raw = client.get_wallet_activity(address, limit=page_size, offset=page * page_size)
        if not raw:
            break
        parsed = [parse_activity_row(r) for r in raw]

        if since_timestamp is not None:
            fresh = [a for a in parsed if a.timestamp > since_timestamp]
            collected.extend(fresh)
            if len(fresh) < len(parsed):
                # Hit the watermark — older rows below this are already stored.
                break
        else:
            collected.extend(parsed)

        if len(raw) < page_size:
            break
    return collected


def run_wallets(
    client: PolymarketClient,
    stores: _Stores,
    *,
    addresses: list[str],
    activity_page_size: int = 500,
    max_pages_per_wallet: int = 20,
    full_backfill: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> tuple[int, int, int]:
    """
    Returns (activity_written, positions_written, values_written).

    full_backfill=True ignores the per-wallet checkpoint and walks every page
    up to max_pages. Default (False) only fetches events newer than the last
    seen timestamp — the steady-state path.
    """
    total_activity = total_positions = total_values = 0
    total = len(addresses)
    for i, addr in enumerate(addresses):
        if progress_cb is not None:
            progress_cb({
                "stage": "wallets",
                "current": i + 1,
                "total": total,
                "wallet": addr,
            })
        try:
            since = None if full_backfill else stores.checkpoints.get_last_timestamp(addr)

            activity = _paginate_activity(
                client,
                addr,
                page_size=activity_page_size,
                max_pages=max_pages_per_wallet,
                since_timestamp=since,
            )
            total_activity += stores.activity.write_activity(activity)
            if activity:
                # Activity is newest-first; bump the watermark to the max we saw.
                max_ts = max(a.timestamp for a in activity)
                stores.checkpoints.set_last_timestamp(addr, max_ts)

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
                "wallet wallet=%s since=%s activity_new=%d positions=%d value_usdc=%.2f",
                addr,
                since,
                len(activity),
                len(parsed_positions),
                value.value_usdc,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("wallet_failed wallet=%s error=%s", addr, exc)
    return total_activity, total_positions, total_values


def seed_watchlist_from_leaderboard(
    client: PolymarketClient,
    stores: _Stores,
    watchlist_path: Path,
    *,
    top_n_profit: int = 50,
    top_n_volume: int = 50,
) -> list[str]:
    """
    Fetch profit + volume leaderboards, union the wallets, and write them to
    the watchlist file. Existing addresses in the file are preserved.

    Returns the merged watchlist (sorted, deduped, lowercase).
    """
    # N<=0 means "skip this side entirely". The Polymarket API treats limit=0
    # as "no limit" and returns its default page (~50), which would silently
    # pull wallets you explicitly asked to exclude.
    profit_entries: list[PolymarketLeaderboardEntry] = []
    if top_n_profit > 0:
        raw_profit = client.get_leaderboard(metric="profit", window="all", limit=top_n_profit)
        profit_entries = [
            parse_leaderboard_row(r, metric="profit", window="all") for r in raw_profit
        ]
        stores.leaderboard.write_leaderboard(profit_entries)

    volume_entries: list[PolymarketLeaderboardEntry] = []
    if top_n_volume > 0:
        raw_volume = client.get_leaderboard(metric="volume", window="all", limit=top_n_volume)
        volume_entries = [
            parse_leaderboard_row(r, metric="volume", window="all") for r in raw_volume
        ]
        stores.leaderboard.write_leaderboard(volume_entries)

    new_wallets = {e.proxy_wallet for e in profit_entries + volume_entries if e.proxy_wallet}
    existing = set(_load_watchlist(watchlist_path)) if watchlist_path.exists() else set()
    merged = sorted(new_wallets | existing)

    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist_path.write_text(
        "\n".join(
            ["# Polymarket wallet watchlist — auto-seeded + manual additions"]
            + merged
        )
        + "\n",
        encoding="utf-8",
    )
    log.info(
        "watchlist_seeded profit=%d volume=%d total=%d new=%d path=%s",
        len(profit_entries),
        len(volume_entries),
        len(merged),
        len(new_wallets - existing),
        watchlist_path,
    )
    return merged


def run_markets(
    client: PolymarketClient,
    stores: _Stores,
    *,
    closed: bool | None,
    pages: int,
    page_size: int,
    order: str = "endDate",
    ascending: bool = False,
) -> int:
    total = 0
    for page in range(pages):
        raw = client.get_markets(
            closed=closed,
            limit=page_size,
            offset=page * page_size,
            order=order,
            ascending=ascending,
        )
        if not raw:
            break
        parsed = [parse_market_row(r) for r in raw]
        total += stores.markets.write_markets(parsed)
        log.info("markets page=%d fetched=%d total_new=%d", page, len(raw), total)
        if len(raw) < page_size:
            break
    return total


def run_markets_backfill_from_activity(
    client: PolymarketClient, stores: _Stores
) -> int:
    """
    Find every condition_id referenced in the local activity store, look up
    which ones we DON'T already have in the markets store, and fetch them
    directly by condition_id. This closes the join gap for skill scoring.
    """
    activity_rows = _read_jsonl(stores.activity_path)
    activity_conds = {
        str(r.get("condition_id", "")) for r in activity_rows
        if r.get("type") == "TRADE" and r.get("condition_id")
    }
    if not activity_conds:
        return 0

    existing_market_rows = _read_jsonl(stores.markets_path)
    known_conds = {str(r.get("condition_id", "")) for r in existing_market_rows}
    missing = sorted(activity_conds - known_conds)
    if not missing:
        log.info("markets_backfill missing=0 (all activity condition_ids already cached)")
        return 0

    log.info(
        "markets_backfill activity_conds=%d known=%d missing=%d",
        len(activity_conds), len(known_conds), len(missing),
    )
    raw = client.get_markets_by_condition_ids(missing)
    parsed = [parse_market_row(r) for r in raw]
    written = stores.markets.write_markets(parsed)
    log.info("markets_backfill fetched=%d written=%d", len(raw), written)
    return written


# ── JSONL readers (for the enrichment pass) ──────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _load_activity_records(path: Path) -> list[PolymarketActivity]:
    out: list[PolymarketActivity] = []
    for row in _read_jsonl(path):
        try:
            out.append(
                PolymarketActivity(
                    proxy_wallet=str(row.get("proxy_wallet", "")),
                    timestamp=int(row.get("timestamp", 0) or 0),
                    condition_id=str(row.get("condition_id", "")),
                    type=str(row.get("type", "")),
                    side=str(row.get("side", "")),
                    size=_as_float(row.get("size")),
                    usdc_size=_as_float(row.get("usdc_size")),
                    price=_as_float(row.get("price")),
                    outcome_index=int(row.get("outcome_index", 0) or 0),
                    outcome=str(row.get("outcome", "")),
                    slug=str(row.get("slug", "")),
                    title=str(row.get("title", "")),
                    event_slug=str(row.get("event_slug", "")),
                    transaction_hash=str(row.get("transaction_hash", "")),
                    name=str(row.get("name", "")),
                    pseudonym=str(row.get("pseudonym", "")),
                )
            )
        except (TypeError, ValueError) as exc:
            log.warning("activity_row_parse_failed error=%s row_keys=%s", exc, list(row.keys())[:5])
    return out


def _load_market_records(path: Path) -> list[PolymarketMarket]:
    out: list[PolymarketMarket] = []
    for row in _read_jsonl(path):
        try:
            outcomes_raw = row.get("outcomes")
            prices_raw = row.get("outcome_prices")
            out.append(
                PolymarketMarket(
                    gamma_id=str(row.get("gamma_id", "")),
                    condition_id=str(row.get("condition_id", "")),
                    slug=str(row.get("slug", "")),
                    question=str(row.get("question", "")),
                    category=str(row.get("category", "")),
                    end_date=str(row.get("end_date", "")),
                    outcomes=[str(o) for o in outcomes_raw] if isinstance(outcomes_raw, list) else [],
                    outcome_prices=[_as_float(p) for p in prices_raw] if isinstance(prices_raw, list) else [],
                    volume_usdc=_as_float(row.get("volume_usdc")),
                    liquidity_usdc=_as_float(row.get("liquidity_usdc")),
                    closed=bool(row.get("closed", False)),
                    active=bool(row.get("active", False)),
                    last_trade_price=_as_float_or_none(row.get("last_trade_price")),
                    best_bid=_as_float_or_none(row.get("best_bid")),
                    best_ask=_as_float_or_none(row.get("best_ask")),
                    fetched_at=str(row.get("fetched_at", "")),
                )
            )
        except (TypeError, ValueError) as exc:
            log.warning("market_row_parse_failed error=%s", exc)
    return out


def _load_leaderboard_records(path: Path) -> list[PolymarketLeaderboardEntry]:
    out: list[PolymarketLeaderboardEntry] = []
    for row in _read_jsonl(path):
        try:
            out.append(
                PolymarketLeaderboardEntry(
                    proxy_wallet=str(row.get("proxy_wallet", "")),
                    name=str(row.get("name", "")),
                    pseudonym=str(row.get("pseudonym", "")),
                    amount_usdc=_as_float(row.get("amount_usdc")),
                    metric=str(row.get("metric", "")),
                    window=str(row.get("window", "")),
                    fetched_at=str(row.get("fetched_at", "")),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


# ── Polymarket → Kalshi market matching ──────────────────────────────────────

# Kalshi "multi-game" / "multi-vendor equity" parlay tickers concatenate every
# leg into one title (e.g. "yes A, yes B, yes C"), which produces high-recall
# but low-precision TF-IDF matches against any Polymarket market that mentions
# one of the legs. They're not single-event markets in the sense the matcher
# assumes, so we exclude them entirely.
#
# Filtering happens here at the adapter — the raw JSONL is preserved (still
# useful for Kalshi-side orderflow analyses) and the matcher itself stays
# source-agnostic. Add new prefixes here if other parlay families appear.
_KALSHI_PARLAY_PREFIXES: tuple[str, ...] = ("KXMVE",)


def _is_kalshi_parlay(ticker: str) -> bool:
    return ticker.upper().startswith(_KALSHI_PARLAY_PREFIXES)


def _kalshi_to_normalized(market: KalshiMarket) -> NormalizedMarket:
    """Map a KalshiMarket to the matcher's source-agnostic adapter."""
    title = market.title or market.subtitle or market.yes_sub_title
    return NormalizedMarket(
        source="kalshi",
        identifier=market.ticker,
        slug="",
        title=title,
        category=market.category,
        end_date=market.expiration_time or market.close_time,
    )


def _polymarket_to_normalized(market: PolymarketMarket) -> NormalizedMarket:
    return NormalizedMarket(
        source="polymarket",
        identifier=market.condition_id,
        slug=market.slug,
        title=market.question,
        category=market.category,
        end_date=market.end_date,
    )


def run_fetch_kalshi_markets(stores: _Stores, *, status: str, max_pages: int) -> int:
    markets = fetch_kalshi_markets(status=status, max_pages=max_pages)
    written = write_kalshi_markets_jsonl(markets, stores.kalshi_markets_path)
    log.info("kalshi_markets_written count=%d status=%s path=%s", written, status, stores.kalshi_markets_path)
    return written


def run_match_markets(stores: _Stores, *, config: MatchConfig | None = None) -> int:
    """
    Load Kalshi + Polymarket markets from disk, run the matcher, persist
    market_links.jsonl with manual decisions preserved.
    """
    kalshi_raw = load_kalshi_markets_jsonl(stores.kalshi_markets_path)
    if not kalshi_raw:
        log.error(
            "no kalshi markets at %s — run 'fetch-kalshi-markets' first",
            stores.kalshi_markets_path,
        )
        return 0
    poly_raw = _load_market_records(stores.markets_path)
    if not poly_raw:
        log.error("no polymarket markets — run 'markets' first")
        return 0

    kalshi_filtered = [m for m in kalshi_raw if not _is_kalshi_parlay(m.ticker)]
    excluded = len(kalshi_raw) - len(kalshi_filtered)
    if excluded:
        log.info(
            "match_markets excluded_parlays=%d prefixes=%s",
            excluded, _KALSHI_PARLAY_PREFIXES,
        )

    kalshi_norm = [_kalshi_to_normalized(m) for m in kalshi_filtered]
    poly_norm = [_polymarket_to_normalized(m) for m in poly_raw]
    links = match_markets(kalshi_norm, poly_norm, config=config)
    written = stores.market_links.upsert_links(links)
    log.info(
        "match_markets kalshi=%d polymarket=%d new_or_updated_links=%d total_in_store=%d",
        len(kalshi_norm), len(poly_norm), written, len(stores.market_links.load_links()),
    )
    return written


def run_review_matches(stores: _Stores, *, limit: int) -> int:
    """
    Interactive CLI: walk through pending matches, prompt y/n/skip, persist decisions.
    Returns the number of decisions made.
    """
    all_links = stores.market_links.load_links()
    pending = [link for link in all_links if link.status == "pending" and link.matched_by != "manual"]
    if not pending:
        print("No pending matches. Run 'match-markets' first.")
        return 0

    pending.sort(key=lambda link: -link.confidence)
    decisions = 0
    print(f"\n{len(pending)} pending match{'es' if len(pending) != 1 else ''}. Reviewing up to {limit}.\n")
    print("Commands: [y] approve  [n] reject  [s] skip  [q] quit\n")

    for i, link in enumerate(pending[:limit], start=1):
        print(f"--- {i}/{min(len(pending), limit)} (confidence {link.confidence:.3f}) ---")
        print(f"  Kalshi    : {link.kalshi_ticker}")
        print(f"             {link.kalshi_title!r}")
        print(f"             ends {link.kalshi_end_date}")
        print(f"  Polymarket: {link.polymarket_slug}")
        print(f"             {link.polymarket_title!r}")
        print(f"             ends {link.polymarket_end_date}")

        try:
            choice = input("Decision [y/n/s/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            break

        if choice == "q":
            break
        if choice == "s":
            continue
        new_status: str
        if choice == "y":
            new_status = "approved"
        elif choice == "n":
            new_status = "rejected"
        else:
            print("  (unrecognized — treating as skip)")
            continue

        updated = MarketLink(
            kalshi_ticker=link.kalshi_ticker,
            polymarket_condition_id=link.polymarket_condition_id,
            polymarket_slug=link.polymarket_slug,
            kalshi_title=link.kalshi_title,
            polymarket_title=link.polymarket_title,
            kalshi_end_date=link.kalshi_end_date,
            polymarket_end_date=link.polymarket_end_date,
            confidence=link.confidence,
            status=new_status,
            matched_by="manual",
        )
        stores.market_links.upsert_links([updated])
        decisions += 1
        print(f"  -> {new_status}\n")

    log.info("review_matches decisions=%d", decisions)
    return decisions


def run_enrichment(stores: _Stores) -> int:
    """Recompute and overwrite wallet enrichment from the local JSONL stores."""
    activity = _load_activity_records(stores.activity_path)
    markets = _load_market_records(stores.markets_path)
    leaderboard = _load_leaderboard_records(stores.leaderboard_path)
    enrichments = compute_all_enrichment(
        activity=activity, markets=markets, leaderboard=leaderboard
    )
    written = stores.enrichment.write_enrichment(enrichments)
    log.info("enrichment_written wallets=%d", written)
    return written


# ── Deep review state (sweep + prune) ────────────────────────────────────────
#
# The deep-review pipeline polls a much wider universe of wallets than the
# shallow dashboard ingest. To keep steady-state cost bounded we maintain a
# sidecar review-state JSONL keyed by wallet, with a `status` flag that lets
# us archive wallets that go cold without deleting any historical data.

# Default thresholds reused as the wallet protections during prune. These
# mirror /signals/skilled-bets defaults (apps/api/.../routes/skilled_bets.py)
# so anything currently visible in that endpoint can never be archived.
DEEP_SKILLED_MIN_SKILL = 0.8
DEEP_SKILLED_MIN_RESOLVED = 20
DEEP_DEFAULT_DORMANT_DAYS = 90


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_review_state(path: Path) -> dict[str, PolymarketWalletReviewState]:
    """Read the review-state JSONL into a wallet-keyed dict. Missing file → {}."""
    if not path.exists():
        return {}
    out: dict[str, PolymarketWalletReviewState] = {}
    for row in _read_jsonl(path):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        try:
            out[wallet] = PolymarketWalletReviewState(
                proxy_wallet=wallet,
                status=str(row.get("status", "active")) or "active",
                first_seen_at=str(row.get("first_seen_at", "")) or _utcnow_iso(),
                last_leaderboard_seen_at=row.get("last_leaderboard_seen_at") or None,
                last_activity_at=(
                    int(row["last_activity_at"])
                    if row.get("last_activity_at") not in (None, "")
                    else None
                ),
                last_polled_at=row.get("last_polled_at") or None,
                best_rank=(
                    int(row["best_rank"])
                    if row.get("best_rank") not in (None, "")
                    else None
                ),
                appearances=int(row.get("appearances", 0) or 0),
                categories=[str(c) for c in (row.get("categories") or []) if c],
                time_periods=[str(t) for t in (row.get("time_periods") or []) if t],
                orders=[str(o) for o in (row.get("orders") or []) if o],
                sources=[str(s) for s in (row.get("sources") or []) if s],
                archived_at=row.get("archived_at") or None,
                archived_reason=row.get("archived_reason") or None,
            )
        except (TypeError, ValueError) as exc:
            log.warning("review_state_row_parse_failed wallet=%s error=%s", wallet, exc)
    return out


def _write_review_state(
    path: Path, state: dict[str, PolymarketWalletReviewState]
) -> int:
    """Atomic rewrite: serialize all rows to a tempfile, then rename over the
    target path. Prevents a half-written file on Ctrl-C or disk-full."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for wallet in sorted(state):
            row = {
                "proxy_wallet": state[wallet].proxy_wallet,
                "status": state[wallet].status,
                "first_seen_at": state[wallet].first_seen_at,
                "last_leaderboard_seen_at": state[wallet].last_leaderboard_seen_at,
                "last_activity_at": state[wallet].last_activity_at,
                "last_polled_at": state[wallet].last_polled_at,
                "best_rank": state[wallet].best_rank,
                "appearances": state[wallet].appearances,
                "categories": state[wallet].categories,
                "time_periods": state[wallet].time_periods,
                "orders": state[wallet].orders,
                "sources": state[wallet].sources,
                "archived_at": state[wallet].archived_at,
                "archived_reason": state[wallet].archived_reason,
            }
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return len(state)


def _load_skilled_wallets(enrichment_path: Path) -> set[str]:
    """Wallets meeting the API's default skilled cutoff. Protected from prune."""
    skilled: set[str] = set()
    for row in _read_jsonl(enrichment_path):
        skill = _as_float(row.get("skill_likelihood"))
        resolved = int(row.get("resolved_trades", 0) or 0)
        if skill >= DEEP_SKILLED_MIN_SKILL and resolved >= DEEP_SKILLED_MIN_RESOLVED:
            wallet = str(row.get("proxy_wallet", "")).lower()
            if wallet:
                skilled.add(wallet)
    return skilled


def _load_last_activity_by_wallet(enrichment_path: Path) -> dict[str, int]:
    """Most-recent activity timestamp per wallet, sourced from enrichment.
    Wallets not present here are treated as `last_activity_at = None`."""
    out: dict[str, int] = {}
    for row in _read_jsonl(enrichment_path):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        ts = int(row.get("last_activity_at", 0) or 0)
        if ts > out.get(wallet, 0):
            out[wallet] = ts
    return out


def _load_wallets_with_open_positions(positions_path: Path) -> set[str]:
    """Wallets with at least one latest-snapshot position size > 1e-9.

    Mirrors the still-held logic in apps/api/.../services/skilled_bets.py.
    JSONL is append-only so we keep the row with the most recent snapshot_at
    for each (wallet, condition_id, outcome_index).
    """
    latest: dict[tuple[str, str, int], tuple[str, float]] = {}
    for row in _read_jsonl(positions_path):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        key = (
            wallet,
            str(row.get("condition_id", "")),
            int(row.get("outcome_index", 0) or 0),
        )
        snap = str(row.get("snapshot_at", "") or "")
        size = _as_float(row.get("size"))
        prev = latest.get(key)
        if prev is None or snap >= prev[0]:
            latest[key] = (snap, size)
    return {key[0] for key, (_, size) in latest.items() if size > 1e-9}


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
    wa.add_argument("--activity-page-size", type=int, default=500)
    wa.add_argument("--max-pages-per-wallet", type=int, default=20)
    wa.add_argument(
        "--full-backfill",
        action="store_true",
        help="Ignore checkpoint and walk all pages",
    )

    sd = sub.add_parser("seed-watchlist", help="Seed the watchlist from the volume leaderboard")
    # Profit defaults to 0 (skipped): the PnL leaderboard over-represents lucky
    # single-win wallets. Pass --top-n-profit N to opt back in.
    sd.add_argument("--top-n-profit", type=int, default=0)
    sd.add_argument("--top-n-volume", type=int, default=50)

    mk = sub.add_parser("markets", help="Fetch market metadata from Gamma")
    mk.add_argument("--closed", action="store_true", help="Fetch resolved markets")
    mk.add_argument("--active", action="store_true", help="Fetch only active markets")
    mk.add_argument("--pages", type=int, default=5)
    mk.add_argument("--page-size", type=int, default=100)
    mk.add_argument("--order", default="endDate", help="Sort field (endDate, updatedAt, volumeNum)")
    mk.add_argument("--ascending", action="store_true", help="Sort ascending (default: descending)")
    mk.add_argument(
        "--backfill-from-activity",
        action="store_true",
        help="Also fetch any condition_ids present in activity but missing from markets store",
    )

    sub.add_parser(
        "enrichment",
        help="Recompute wallet skill enrichment from the local JSONL stores",
    )

    fk = sub.add_parser("fetch-kalshi-markets", help="Pull Kalshi public markets into kalshi_markets.jsonl")
    fk.add_argument("--status", default="settled", choices=["open", "closed", "settled"])
    fk.add_argument("--max-pages", type=int, default=25)

    mm = sub.add_parser("match-markets", help="Run Polymarket → Kalshi title matcher")
    mm.add_argument("--date-window-days", type=int, default=3)
    mm.add_argument("--auto-approve-threshold", type=float, default=0.75)
    mm.add_argument("--review-threshold", type=float, default=0.35)
    mm.add_argument("--max-per-kalshi", type=int, default=3)

    rv = sub.add_parser("review-matches", help="Interactively approve/reject pending matches")
    rv.add_argument("--limit", type=int, default=50)

    sub.add_parser("all", help="Run seed + watchlist wallets + markets + enrichment in one pass")

    pl = sub.add_parser(
        "pipeline",
        help=(
            "End-to-end Polymarket pipeline (the same orchestrator the web "
            "'Run ingest' button invokes). Seeds wallets across windows, "
            "pulls activity, fetches Kalshi markets, and matches them."
        ),
    )
    pl.add_argument(
        "--window",
        action="append",
        default=[],
        help=(
            "Leaderboard time window to attempt (day/week/month/all). "
            "Repeatable. Defaults to all four if not provided."
        ),
    )
    pl.add_argument("--leaderboard-limit", type=int, default=50)
    pl.add_argument("--skip-kalshi", action="store_true",
                    help="Skip the Kalshi fetch + match step")
    pl.add_argument("--include-profit", action="store_true",
                    help="Also seed from the profit/PnL leaderboard "
                         "(off by default to avoid luck bias)")

    dp = sub.add_parser(
        "deep-pipeline",
        help=(
            "Deep review pass: sweep the full categorized leaderboard matrix, "
            "refresh wallet review-state, prune dormant wallets, and hydrate "
            "the oldest-polled batch of active+pinned wallets."
        ),
    )
    dp.add_argument("--leaderboard-depth", type=int, default=_DEEP_DEFAULT_DEPTH,
                    help="Top-N rows per slice (default 250, max 1000)")
    dp.add_argument("--wallet-batch-size", type=int, default=_DEEP_DEFAULT_BATCH_SIZE,
                    help="Wallets hydrated per invocation")
    dp.add_argument("--activity-pages", type=int, default=_DEEP_DEFAULT_ACTIVITY_PAGES,
                    help="Max activity pages per net-new wallet (existing wallets short-circuit on checkpoint)")
    dp.add_argument("--dormant-days", type=int, default=DEEP_DEFAULT_DORMANT_DAYS,
                    help="Wallets with no activity in this many days are archive candidates")
    dp.add_argument("--recent-trader-limit", type=int, default=_DEEP_DEFAULT_RECENT_TRADERS,
                    help="Max distinct recent on-chain wallets to fold into the sweep")
    dp.add_argument("--no-recent-traders", action="store_true",
                    help="Skip subgraph recent-trader discovery (leaderboard sweep only)")
    dp.add_argument("--skip-kalshi", action="store_true")

    rt = sub.add_parser(
        "recent-traders",
        help="Seed deep-review state with the most-recent on-chain traders (subgraph)",
    )
    rt.add_argument("--limit", type=int, default=_DEEP_DEFAULT_RECENT_TRADERS,
                    help="Max distinct wallets to discover")
    rt.add_argument("--pages", type=int, default=_DEEP_DEFAULT_RECENT_MAX_PAGES,
                    help="Max subgraph pages to walk")

    pw = sub.add_parser(
        "prune-wallets",
        help="Apply dormancy archival to review-state without running a full pipeline",
    )
    pw.add_argument("--dormant-days", type=int, default=DEEP_DEFAULT_DORMANT_DAYS)
    pw.add_argument("--dry-run", action="store_true",
                    help="Report what would be archived without mutating the file")

    pin = sub.add_parser("pin-wallet", help="Mark a wallet as pinned (never archived)")
    pin.add_argument("address")

    unpin = sub.add_parser("unpin-wallet", help="Revert a pinned wallet to active")
    unpin.add_argument("address")

    return parser


@dataclass(slots=True)
class PipelineResult:
    """Counts surfaced back to the API so the UI can summarize the run.

    Deep-pipeline runs populate the trailing counters; shallow runs leave
    them at their default 0.
    """

    windows_attempted: list[str]
    windows_succeeded: list[str]
    leaderboard_entries: int
    wallets_seeded: int
    activity_records: int
    positions: int
    wallet_values: int
    markets_written: int
    markets_backfilled: int
    enrichment_wallets: int
    kalshi_markets: int
    market_links: int
    # Deep-pipeline-only counters (default 0 for shallow runs).
    discovered_this_run: int = 0
    recent_traders_discovered: int = 0
    deep_slices_attempted: int = 0
    deep_slices_succeeded: int = 0
    active_wallets: int = 0
    archived_wallets: int = 0
    pinned_wallets: int = 0
    pruned_this_run: int = 0
    # Set when a deep run completes as a *partial success* (circuit breaker
    # tripped mid-sweep). Surfaced to the UI; exit code stays 0.
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows_attempted": list(self.windows_attempted),
            "windows_succeeded": list(self.windows_succeeded),
            "leaderboard_entries": self.leaderboard_entries,
            "wallets_seeded": self.wallets_seeded,
            "activity_records": self.activity_records,
            "positions": self.positions,
            "wallet_values": self.wallet_values,
            "markets_written": self.markets_written,
            "markets_backfilled": self.markets_backfilled,
            "enrichment_wallets": self.enrichment_wallets,
            "kalshi_markets": self.kalshi_markets,
            "market_links": self.market_links,
            "discovered_this_run": self.discovered_this_run,
            "recent_traders_discovered": self.recent_traders_discovered,
            "deep_slices_attempted": self.deep_slices_attempted,
            "deep_slices_succeeded": self.deep_slices_succeeded,
            "active_wallets": self.active_wallets,
            "archived_wallets": self.archived_wallets,
            "pinned_wallets": self.pinned_wallets,
            "pruned_this_run": self.pruned_this_run,
            "warning": self.warning,
        }


# ── Public in-process orchestrator ───────────────────────────────────────────
#
# The API's "Run ingest" button invokes this directly (no subprocess, no CLI
# argv parsing) so we can stream log lines back to the UI as the pipeline
# progresses. The CLI `main()` below remains for ops who want to run a single
# subcommand from the shell.

_DEFAULT_WINDOWS: tuple[str, ...] = ("day", "week", "month", "all")


def run_pipeline(
    *,
    windows: list[str] | None = None,
    leaderboard_limit: int = 50,
    activity_page_size: int = 500,
    max_pages_per_wallet: int = 20,
    market_pages: int = 5,
    market_page_size: int = 100,
    kalshi_status: str = "open",
    kalshi_max_pages: int = 25,
    skip_kalshi: bool = False,
    include_profit_leaderboard: bool = False,
    client: PolymarketClient | None = None,
    progress_cb: ProgressCallback | None = None,
) -> PipelineResult:
    """End-to-end Polymarket pipeline:

        1. Seed the wallet watchlist by hitting the **volume** leaderboard
           across each configured time window. The profit/PnL leaderboard is
           intentionally excluded — it over-represents wallets that ranked via
           a single lucky win rather than a repeatable edge, biasing the review
           pool. Pass include_profit_leaderboard=True to opt back in. Recent
           on-chain trader discovery lives in run_deep_pipeline, not here: this
           path re-hydrates the entire watchlist every run, so its seed must
           stay bounded. Unsupported windows are skipped with a warning (the
           Polymarket public leaderboard API silently rejects some windows).
        2. Pull each wallet's recent activity, current positions, and value.
        3. Fetch resolved-market metadata (closed=True) for skill scoring,
           plus backfill any condition_ids referenced by activity that
           weren't in the page set.
        4. Recompute per-wallet enrichment (win rate, skill likelihood).
        5. Fetch Kalshi's public /markets and run the Polymarket → Kalshi
           title matcher to populate market_links.jsonl — the join the
           /signals/skilled-bets endpoint uses to surface "mirror this on
           Kalshi" links per row.

    All public APIs hit here are unauthenticated, so this function needs
    zero env-var configuration to run.
    """
    attempts = list(windows or _DEFAULT_WINDOWS)
    stores = _build_stores(_data_dir())
    owns_client = client is None
    client = client or PolymarketClient(PolymarketClientConfig.from_env())

    def _emit(payload: dict[str, Any]) -> None:
        if progress_cb is not None:
            progress_cb(payload)

    try:
        # 1. Seed watchlist across (window × metric)
        metrics = ("profit", "volume") if include_profit_leaderboard else ("volume",)
        log.info(
            "pipeline step=seed_watchlist windows=%s metrics=%s", attempts, metrics
        )
        existing = set(_load_watchlist(_watchlist_path()))
        seeded: set[str] = set(existing)
        leaderboard_entries = 0
        succeeded: list[str] = []
        for i, window in enumerate(attempts):
            _emit({
                "stage": "seed_watchlist",
                "current": i + 1,
                "total": len(attempts),
                "detail": f"window={window}",
            })
            window_ok = False
            for metric in metrics:
                try:
                    raw = client.get_leaderboard(
                        metric=metric, window=window, limit=leaderboard_limit
                    )
                except httpx.HTTPStatusError as exc:
                    log.warning(
                        "leaderboard skip window=%s metric=%s status=%d "
                        "(API rejected this window)",
                        window, metric, exc.response.status_code,
                    )
                    continue
                entries = [
                    parse_leaderboard_row(r, metric=metric, window=window) for r in raw
                ]
                stores.leaderboard.write_leaderboard(entries)
                leaderboard_entries += len(entries)
                for e in entries:
                    if e.proxy_wallet:
                        seeded.add(e.proxy_wallet)
                window_ok = True
            if window_ok:
                succeeded.append(window)

        # Persist the merged watchlist so subsequent runs (and the wallets
        # step below) see the union of auto-seeded + manual additions.
        watchlist_path = _watchlist_path()
        merged = sorted(seeded)
        watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        watchlist_path.write_text(
            "\n".join(
                ["# Polymarket wallet watchlist — auto-seeded + manual additions"]
                + merged,
            ) + "\n",
            encoding="utf-8",
        )
        log.info(
            "watchlist_seeded total=%d new=%d windows_ok=%s",
            len(merged), len(seeded - existing), succeeded,
        )

        # 2. Per-wallet activity/positions/value
        log.info("pipeline step=wallets count=%d", len(merged))
        activity_total, positions_total, values_total = (0, 0, 0)
        if merged:
            _emit({"stage": "wallets", "current": 0, "total": len(merged)})
            activity_total, positions_total, values_total = run_wallets(
                client,
                stores,
                addresses=merged,
                activity_page_size=activity_page_size,
                max_pages_per_wallet=max_pages_per_wallet,
                progress_cb=progress_cb,
            )

        # 3. Polymarket market metadata + activity-backfill
        log.info("pipeline step=markets")
        _emit({"stage": "markets"})
        markets_written = run_markets(
            client, stores,
            closed=True, pages=market_pages, page_size=market_page_size,
        )
        markets_backfilled = run_markets_backfill_from_activity(client, stores)

        # 4. Compute on-chain skill enrichment
        log.info("pipeline step=enrichment")
        _emit({"stage": "enrichment"})
        enrichment_written = run_enrichment(stores)

        # 5. Kalshi mirror: fetch public markets + match
        kalshi_written = 0
        links_written = 0
        if not skip_kalshi:
            log.info("pipeline step=kalshi_markets status=%s", kalshi_status)
            _emit({"stage": "kalshi_markets"})
            try:
                kalshi_written = run_fetch_kalshi_markets(
                    stores, status=kalshi_status, max_pages=kalshi_max_pages,
                )
                log.info("pipeline step=match_markets")
                _emit({"stage": "match_markets"})
                links_written = run_match_markets(stores)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "kalshi mirror step failed (non-fatal): %s — skilled bets "
                    "will still show but without Kalshi cross-references",
                    exc,
                )

        result = PipelineResult(
            windows_attempted=attempts,
            windows_succeeded=succeeded,
            leaderboard_entries=leaderboard_entries,
            wallets_seeded=len(merged),
            activity_records=activity_total,
            positions=positions_total,
            wallet_values=values_total,
            markets_written=markets_written,
            markets_backfilled=markets_backfilled,
            enrichment_wallets=enrichment_written,
            kalshi_markets=kalshi_written,
            market_links=links_written,
        )
        log.info("pipeline complete %s", result.to_dict())
        return result
    finally:
        if owns_client:
            client.close()


# ── Deep review pipeline ─────────────────────────────────────────────────────
#
# Separate orchestrator from run_pipeline(): sweeps the full categorized
# leaderboard matrix, maintains a sidecar review-state JSONL, and hydrates
# only `active` + `pinned` wallets in batches of 500 oldest-polled-first.
# The shallow run_pipeline() above remains the dashboard's "Run ingest" path.

_DEEP_DEFAULT_DEPTH = 250            # top-N per slice
_DEEP_DEFAULT_BATCH_SIZE = 500       # wallets hydrated per invocation
_DEEP_DEFAULT_ACTIVITY_PAGES = 5     # cap for net-new wallets
_DEEP_LIMIT_PER_REQUEST = 50         # API max
_DEEP_DEFAULT_ORDERS: tuple[str, ...] = ("VOL",)  # PnL excluded — see run_deep_leaderboard
_DEEP_DEFAULT_RECENT_TRADERS = 1000  # distinct recent on-chain wallets folded into the sweep
_DEEP_DEFAULT_RECENT_PAGE_SIZE = 500
_DEEP_DEFAULT_RECENT_MAX_PAGES = 20
_DEEP_BREAKER_THRESHOLD = 6          # consecutive degraded slices → abort sweep


@dataclass(slots=True)
class _WalletSweepMeta:
    appearances: int = 0
    categories: set[str] = field(default_factory=set)
    time_periods: set[str] = field(default_factory=set)
    orders: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)  # "leaderboard" | "subgraph"
    best_rank: int | None = None  # 1-indexed; lower = better


@dataclass(slots=True)
class _DeepSweepResult:
    discovered: set[str]
    leaderboard_entries: int
    slices_attempted: int
    slices_succeeded: int
    per_wallet: dict[str, _WalletSweepMeta]
    # Set when the circuit breaker aborts the sweep before the full matrix is
    # covered. A partial sweep MUST NOT be used to prune review-state.
    aborted_early: bool = False
    abort_reason: str | None = None


def run_deep_leaderboard(
    client: PolymarketClient,
    stores: _Stores,
    *,
    depth: int = _DEEP_DEFAULT_DEPTH,
    orders: tuple[str, ...] = _DEEP_DEFAULT_ORDERS,
    breaker_threshold: int = _DEEP_BREAKER_THRESHOLD,
    progress_cb: ProgressCallback | None = None,
) -> _DeepSweepResult:
    """Sweep the leaderboard matrix and return the union of wallets.

    Iterates 10 categories × 4 time periods × len(orders) orders ×
    ceil(depth/50) offsets. `orders` defaults to ("VOL",): the PnL/profit
    ranking is excluded because it over-represents lucky single-win wallets,
    biasing the review pool (pass orders=("PNL", "VOL") to include it).

    Per-slice *4xx* errors mean an unsupported category×period combo: they
    are logged and skipped, sweep continues. Per-slice *5xx/429* or
    *transport* errors mean the upstream is unhealthy; once
    `breaker_threshold` such slices fail consecutively the sweep aborts
    early (circuit breaker) and returns the partial result with
    `aborted_early=True` — callers MUST NOT prune review-state against a
    partial sweep. Empty slices still count as 'attempted' but not
    'succeeded'. Raw rows are appended to polymarket_leaderboard.jsonl
    (dedupe is downstream's concern).
    """
    bad = [o for o in orders if o not in PolymarketClient.LEADERBOARD_ORDERS]
    if bad:
        raise ValueError(f"unknown order(s) {bad!r}")

    discovered: set[str] = set()
    per_wallet: dict[str, _WalletSweepMeta] = {}
    leaderboard_entries = 0
    slices_attempted = 0
    slices_succeeded = 0
    consecutive_degraded = 0
    aborted_early = False
    abort_reason: str | None = None

    # Flatten the matrix so the circuit breaker can abort the whole sweep
    # with a single `break` (Python has no labeled break across nested
    # loops). Iteration order — category outer, order inner — matches the
    # original nested loops.
    slice_dims = [
        (category, time_period, order_by)
        for category in PolymarketClient.LEADERBOARD_CATEGORIES
        for time_period in PolymarketClient.LEADERBOARD_TIME_PERIODS
        for order_by in orders
    ]
    total_slice_dims = len(slice_dims)

    for dim_idx, (category, time_period, order_by) in enumerate(
        slice_dims, start=1,
    ):
        if progress_cb is not None:
            progress_cb({
                "stage": "deep_leaderboard",
                "current": dim_idx,
                "total": total_slice_dims,
                "detail": f"{category}/{time_period}/{order_by}",
            })
        slice_ok = False
        slice_degraded = False
        for offset in range(0, depth, _DEEP_LIMIT_PER_REQUEST):
            slices_attempted += 1
            page_size = min(_DEEP_LIMIT_PER_REQUEST, depth - offset)
            try:
                raw = client.get_trader_leaderboard_rankings(
                    category=category,
                    time_period=time_period,
                    order_by=order_by,
                    limit=page_size,
                    offset=offset,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                log.warning(
                    "deep_leaderboard skip category=%s time_period=%s order_by=%s "
                    "offset=%d status=%d",
                    category, time_period, order_by, offset, status,
                )
                # 5xx/429 = unhealthy upstream → feeds the breaker. A plain
                # 4xx = this slice combo is unsupported → skip without
                # penalty (matches the existing "skip 4xx" contract).
                if status >= 500 or status == 429:
                    slice_degraded = True
                break  # bail out of offsets for this slice
            except httpx.TransportError as exc:
                # Connection refused / timeout / protocol error: upstream is
                # unreachable, not merely rejecting one slice. Always
                # counts as degraded. Previously these propagated uncaught
                # and killed the whole sweep on the first blip.
                log.warning(
                    "deep_leaderboard transport_error category=%s time_period=%s "
                    "order_by=%s offset=%d error=%s",
                    category, time_period, order_by, offset, exc,
                )
                slice_degraded = True
                break
            if not raw:
                # First-page empty — slice is valid but exhausted.
                if offset == 0:
                    slice_ok = True
                break
            slices_succeeded += 1
            slice_ok = True

            # Map the new endpoint's (order_by, time_period) onto the
            # legacy (metric, window) JSONL schema so downstream
            # enrichment can keep reading the same file.
            metric = "profit" if order_by == "PNL" else "volume"
            window = time_period.lower()
            entries = [
                parse_leaderboard_row(r, metric=metric, window=window)
                for r in raw
            ]
            stores.leaderboard.write_leaderboard(entries)
            leaderboard_entries += len(entries)

            for i, entry in enumerate(entries):
                wallet = entry.proxy_wallet
                if not wallet:
                    continue
                discovered.add(wallet)
                rank = offset + i + 1
                meta = per_wallet.setdefault(wallet, _WalletSweepMeta())
                meta.appearances += 1
                meta.categories.add(category)
                meta.time_periods.add(time_period)
                meta.orders.add(order_by)
                meta.sources.add("leaderboard")
                if meta.best_rank is None or rank < meta.best_rank:
                    meta.best_rank = rank

            if len(raw) < page_size:
                break  # exhausted this slice

        if slice_degraded:
            consecutive_degraded += 1
            if consecutive_degraded >= breaker_threshold:
                aborted_early = True
                abort_reason = (
                    "API degraded — stopped early after "
                    f"{consecutive_degraded} consecutive failing leaderboard "
                    f"slices (last: {category}/{time_period}/{order_by})"
                )
                log.error(
                    "deep_leaderboard circuit_breaker_tripped: %s",
                    abort_reason,
                )
                break
        elif slice_ok:
            consecutive_degraded = 0

        if not slice_ok:
            log.info(
                "deep_leaderboard slice_failed category=%s time_period=%s order_by=%s",
                category, time_period, order_by,
            )

    log.info(
        "deep_leaderboard complete discovered=%d entries=%d slices_attempted=%d "
        "slices_succeeded=%d aborted_early=%s",
        len(discovered), leaderboard_entries, slices_attempted, slices_succeeded,
        aborted_early,
    )
    return _DeepSweepResult(
        discovered=discovered,
        leaderboard_entries=leaderboard_entries,
        slices_attempted=slices_attempted,
        slices_succeeded=slices_succeeded,
        per_wallet=per_wallet,
        aborted_early=aborted_early,
        abort_reason=abort_reason,
    )


def run_recent_traders(
    client: PolymarketClient,
    *,
    limit: int = _DEEP_DEFAULT_RECENT_TRADERS,
    page_size: int = _DEEP_DEFAULT_RECENT_PAGE_SIZE,
    max_pages: int = _DEEP_DEFAULT_RECENT_MAX_PAGES,
) -> list[str]:
    """Most-recently-active wallet addresses from the orderbook subgraph.

    Thin wrapper over the client method so the deep pipeline and the
    `recent-traders` CLI share one entry point + log line. This is the
    de-biasing source: it surfaces wallets actively trading on-chain right
    now, independent of whether they ever ranked on a leaderboard.
    """
    wallets = client.get_recent_trader_wallets(
        max_wallets=limit, page_size=page_size, max_pages=max_pages,
    )
    log.info("recent_traders discovered=%d limit=%d", len(wallets), limit)
    return wallets


def _fold_recent_traders_into_sweep(
    sweep: _DeepSweepResult, wallets: list[str]
) -> None:
    """Union subgraph-discovered wallets into a leaderboard sweep result so the
    rest of the deep pipeline (review-state refresh, prune protection,
    hydration cursor) treats them uniformly. Wallets also seen on the
    leaderboard simply gain an extra 'subgraph' source; net-new ones get a
    meta carrying no leaderboard appearances."""
    for addr in wallets:
        sweep.discovered.add(addr)
        meta = sweep.per_wallet.setdefault(addr, _WalletSweepMeta())
        meta.sources.add("subgraph")


def _merge_unique(existing: list[str], incoming: set[str]) -> list[str]:
    """Sorted, deduped union — keeps the review-state file deterministic."""
    return sorted(set(existing) | incoming)


def _apply_sweep_to_review_state(
    state: dict[str, PolymarketWalletReviewState],
    sweep: _DeepSweepResult,
) -> int:
    """Mutate `state` in place to record sweep observations. Returns the
    count of wallets newly added (not previously in state)."""
    now = _utcnow_iso()
    added = 0
    for wallet, meta in sweep.per_wallet.items():
        record = state.get(wallet)
        if record is None:
            record = PolymarketWalletReviewState(
                proxy_wallet=wallet,
                status="active",
                first_seen_at=now,
            )
            state[wallet] = record
            added += 1
        # Pinned wallets stay pinned; previously-archived wallets seen again
        # are reactivated.
        if record.status == "archived":
            record.status = "active"
            record.archived_at = None
            record.archived_reason = None
        # Only a genuine leaderboard appearance bumps the leaderboard-seen
        # clock; a wallet surfaced purely from recent on-chain fills hasn't
        # been "seen on a leaderboard".
        if "leaderboard" in meta.sources:
            record.last_leaderboard_seen_at = now
        record.appearances += meta.appearances
        record.categories = _merge_unique(record.categories, meta.categories)
        record.time_periods = _merge_unique(record.time_periods, meta.time_periods)
        record.orders = _merge_unique(record.orders, meta.orders)
        record.sources = _merge_unique(record.sources, meta.sources)
        if meta.best_rank is not None and (
            record.best_rank is None or meta.best_rank < record.best_rank
        ):
            record.best_rank = meta.best_rank
    return added


def prune_review_state(
    *,
    state: dict[str, PolymarketWalletReviewState],
    discovered: set[str],
    enrichment_path: Path,
    positions_path: Path,
    dormant_days: int = DEEP_DEFAULT_DORMANT_DAYS,
    dry_run: bool = False,
) -> int:
    """Archive dormant wallets that are absent from the latest sweep AND lack
    any protection. Returns the count of wallets newly archived (or that
    *would* be archived under dry_run).

    Protection rules (any one keeps a wallet `active`):
      - status == "pinned"
      - skill_likelihood >= 0.8 AND resolved_trades >= 20 (matches the
        API's default skilled-bets cutoff)
      - any open position with size > 1e-9
      - present in `discovered`
    """
    skilled = _load_skilled_wallets(enrichment_path)
    open_positions = _load_wallets_with_open_positions(positions_path)
    last_activity = _load_last_activity_by_wallet(enrichment_path)
    now = datetime.now(timezone.utc)
    cutoff_ts = int((now - timedelta(days=dormant_days)).timestamp())
    archived = 0

    for wallet, record in state.items():
        if record.status == "pinned":
            continue
        if record.status == "archived":
            continue
        if wallet in discovered:
            continue
        if wallet in skilled:
            continue
        if wallet in open_positions:
            continue
        # Last-known activity timestamp: prefer the in-state value (deep
        # pipeline writes it after hydration), fall back to enrichment.
        last_ts = record.last_activity_at or last_activity.get(wallet)
        if last_ts is not None and last_ts > cutoff_ts:
            continue
        # Dormant + undiscovered + unprotected → archive.
        if not dry_run:
            record.status = "archived"
            record.archived_at = _utcnow_iso()
            record.archived_reason = "dormant_and_undiscovered"
        archived += 1

    log.info(
        "prune_review_state archived=%d dormant_days=%d skilled=%d open_positions=%d dry_run=%s",
        archived, dormant_days, len(skilled), len(open_positions), dry_run,
    )
    return archived


def _pick_hydration_batch(
    state: dict[str, PolymarketWalletReviewState], *, batch_size: int
) -> list[str]:
    """Oldest-polled-first cursor. Wallets with no last_polled_at sort first
    so net-new wallets are always picked up promptly."""
    candidates = [
        record for record in state.values()
        if record.status in {"active", "pinned"}
    ]
    candidates.sort(key=lambda r: (r.last_polled_at or "", r.proxy_wallet))
    return [r.proxy_wallet for r in candidates[:batch_size]]


def run_deep_pipeline(
    *,
    leaderboard_depth: int = _DEEP_DEFAULT_DEPTH,
    leaderboard_orders: tuple[str, ...] = _DEEP_DEFAULT_ORDERS,
    seed_recent_traders: bool = True,
    recent_trader_limit: int = _DEEP_DEFAULT_RECENT_TRADERS,
    recent_trader_page_size: int = _DEEP_DEFAULT_RECENT_PAGE_SIZE,
    recent_trader_max_pages: int = _DEEP_DEFAULT_RECENT_MAX_PAGES,
    wallet_batch_size: int = _DEEP_DEFAULT_BATCH_SIZE,
    activity_pages: int = _DEEP_DEFAULT_ACTIVITY_PAGES,
    activity_page_size: int = 500,
    dormant_days: int = DEEP_DEFAULT_DORMANT_DAYS,
    market_pages: int = 5,
    market_page_size: int = 100,
    kalshi_status: str = "open",
    kalshi_max_pages: int = 25,
    skip_kalshi: bool = False,
    client: PolymarketClient | None = None,
    progress_cb: ProgressCallback | None = None,
) -> PipelineResult:
    """End-to-end deep review:

      1. Sweep the categorized leaderboard matrix (top-N per slice). `orders`
         defaults to VOL-only — the PnL ranking is dropped to keep lucky
         single-win wallets out of the seed.
      1b. Fold in the most-recent on-chain traders from the orderbook subgraph
         (de-biased discovery: wallets trading now, regardless of any
         leaderboard standing). Disable with seed_recent_traders=False.
      2. Refresh review-state: add new wallets, bump observation metadata,
         reactivate any previously-archived wallet that reappears.
      3. Prune: archive wallets that are dormant AND absent from the latest
         sweep AND not protected by pin/skill/open-position.
      4. Hydrate the oldest-polled batch of active+pinned wallets.
      5. Refresh markets, enrichment, and (optionally) the Kalshi mirror.
    """
    stores = _build_stores(_data_dir())
    review_path = _review_state_path()
    state = _load_review_state(review_path)
    owns_client = client is None
    client = client or PolymarketClient(PolymarketClientConfig.from_env())

    def _emit(payload: dict[str, Any]) -> None:
        if progress_cb is not None:
            progress_cb(payload)

    try:
        # 1. Deep matrix sweep
        log.info(
            "deep_pipeline step=deep_leaderboard depth=%d orders=%s",
            leaderboard_depth, leaderboard_orders,
        )
        sweep = run_deep_leaderboard(
            client, stores, depth=leaderboard_depth,
            orders=leaderboard_orders, progress_cb=progress_cb,
        )

        # 1b. Fold in recent on-chain traders (de-biased discovery source).
        # Non-fatal: a subgraph blip should not kill an otherwise-good run,
        # and a sweep that just tripped the breaker shouldn't get killed
        # here by the same outage.
        recent_count = 0
        if seed_recent_traders:
            log.info(
                "deep_pipeline step=recent_traders limit=%d", recent_trader_limit
            )
            _emit({"stage": "recent_traders"})
            try:
                recent = run_recent_traders(
                    client,
                    limit=recent_trader_limit,
                    page_size=recent_trader_page_size,
                    max_pages=recent_trader_max_pages,
                )
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                # ValueError covers GraphQL query errors (e.g. a server-side
                # subgraph statement-timeout), which arrive as an HTTP 200 body
                # and so never tripped the client's status-based retry.
                log.warning(
                    "recent_traders failed (non-fatal): %s — sweep proceeds "
                    "without subgraph wallets",
                    exc,
                )
                recent = []
            _fold_recent_traders_into_sweep(sweep, recent)
            recent_count = len(recent)

        # 2. Apply sweep observations to review-state
        log.info("deep_pipeline step=refresh_review_state")
        _emit({"stage": "refresh_review_state"})
        _apply_sweep_to_review_state(state, sweep)

        # 3. Prune dormant + undiscovered + unprotected wallets — but ONLY
        # if the sweep actually covered the matrix. A circuit-breaker abort
        # (or a zero-discovery sweep) leaves `sweep.discovered` partial;
        # pruning against it would wrongly archive every wallet that was
        # simply never reached before the upstream went unhealthy.
        if sweep.aborted_early or not sweep.discovered:
            log.warning(
                "deep_pipeline skip_prune aborted_early=%s discovered=%d — "
                "partial sweep, pruning would over-archive",
                sweep.aborted_early, len(sweep.discovered),
            )
            pruned_this_run = 0
        else:
            log.info("deep_pipeline step=prune dormant_days=%d", dormant_days)
            _emit({"stage": "prune_review_state"})
            data_dir = _data_dir()
            pruned_this_run = prune_review_state(
                state=state,
                discovered=sweep.discovered,
                enrichment_path=data_dir / "polymarket_wallet_enrichment.jsonl",
                positions_path=data_dir / "polymarket_positions.jsonl",
                dormant_days=dormant_days,
            )

        # 4. Pick + hydrate the oldest-polled batch
        batch = _pick_hydration_batch(state, batch_size=wallet_batch_size)
        log.info(
            "deep_pipeline step=hydrate batch_size=%d (of %d active+pinned)",
            len(batch),
            sum(
                1 for r in state.values()
                if r.status in {"active", "pinned"}
            ),
        )
        activity_total = positions_total = values_total = 0
        if batch:
            _emit({"stage": "wallets", "current": 0, "total": len(batch)})
            activity_total, positions_total, values_total = run_wallets(
                client,
                stores,
                addresses=batch,
                activity_page_size=activity_page_size,
                max_pages_per_wallet=activity_pages,
                progress_cb=progress_cb,
            )
            # Stamp last_polled_at on every wallet in the batch so the cursor
            # advances even if individual fetches errored inside run_wallets.
            polled_at = _utcnow_iso()
            for wallet in batch:
                record = state.get(wallet)
                if record is not None:
                    record.last_polled_at = polled_at

            # Pull the freshest last_activity_at from the just-written
            # checkpoints so the next prune sees current data.
            for wallet in batch:
                ts = stores.checkpoints.get_last_timestamp(wallet)
                if ts is not None:
                    record = state.get(wallet)
                    if record is not None and (
                        record.last_activity_at is None
                        or ts > record.last_activity_at
                    ):
                        record.last_activity_at = ts

        # Persist the updated review-state (atomic rewrite).
        _write_review_state(review_path, state)

        # 5. Markets + enrichment + Kalshi mirror (same as shallow pipeline).
        log.info("deep_pipeline step=markets")
        _emit({"stage": "markets"})
        markets_written = run_markets(
            client, stores,
            closed=True, pages=market_pages, page_size=market_page_size,
        )
        markets_backfilled = run_markets_backfill_from_activity(client, stores)

        log.info("deep_pipeline step=enrichment")
        _emit({"stage": "enrichment"})
        enrichment_written = run_enrichment(stores)

        kalshi_written = 0
        links_written = 0
        if not skip_kalshi:
            log.info("deep_pipeline step=kalshi_markets status=%s", kalshi_status)
            _emit({"stage": "kalshi_markets"})
            try:
                kalshi_written = run_fetch_kalshi_markets(
                    stores, status=kalshi_status, max_pages=kalshi_max_pages,
                )
                log.info("deep_pipeline step=match_markets")
                _emit({"stage": "match_markets"})
                links_written = run_match_markets(stores)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "kalshi mirror step failed (non-fatal): %s — skilled bets "
                    "will still show but without Kalshi cross-references",
                    exc,
                )

        active = sum(1 for r in state.values() if r.status == "active")
        archived = sum(1 for r in state.values() if r.status == "archived")
        pinned = sum(1 for r in state.values() if r.status == "pinned")
        result = PipelineResult(
            windows_attempted=[],  # deep sweep uses the full matrix, not windows
            windows_succeeded=[],
            leaderboard_entries=sweep.leaderboard_entries,
            wallets_seeded=len(state),
            activity_records=activity_total,
            positions=positions_total,
            wallet_values=values_total,
            markets_written=markets_written,
            markets_backfilled=markets_backfilled,
            enrichment_wallets=enrichment_written,
            kalshi_markets=kalshi_written,
            market_links=links_written,
            discovered_this_run=len(sweep.discovered),
            recent_traders_discovered=recent_count,
            deep_slices_attempted=sweep.slices_attempted,
            deep_slices_succeeded=sweep.slices_succeeded,
            active_wallets=active,
            archived_wallets=archived,
            pinned_wallets=pinned,
            pruned_this_run=pruned_this_run,
            warning=sweep.abort_reason,
        )
        log.info("deep_pipeline complete %s", result.to_dict())
        return result
    finally:
        if owns_client:
            client.close()


def run_recent_traders_seed(
    *,
    limit: int = _DEEP_DEFAULT_RECENT_TRADERS,
    page_size: int = _DEEP_DEFAULT_RECENT_PAGE_SIZE,
    max_pages: int = _DEEP_DEFAULT_RECENT_MAX_PAGES,
    client: PolymarketClient | None = None,
) -> dict[str, int]:
    """CLI entry point for `recent-traders`: pull the most-recent on-chain
    traders and merge them into the deep-review state (so the next deep
    hydration picks them up) without running a full sweep. Reuses
    `_apply_sweep_to_review_state` by wrapping the addresses in a synthetic
    subgraph-only sweep result."""
    review_path = _review_state_path()
    state = _load_review_state(review_path)
    owns_client = client is None
    client = client or PolymarketClient(PolymarketClientConfig.from_env())
    try:
        wallets = run_recent_traders(
            client, limit=limit, page_size=page_size, max_pages=max_pages,
        )
    finally:
        if owns_client:
            client.close()

    sweep = _DeepSweepResult(
        discovered=set(wallets),
        leaderboard_entries=0,
        slices_attempted=0,
        slices_succeeded=0,
        per_wallet={w: _WalletSweepMeta(sources={"subgraph"}) for w in wallets},
    )
    added = _apply_sweep_to_review_state(state, sweep)
    _write_review_state(review_path, state)
    active = sum(1 for r in state.values() if r.status == "active")
    log.info(
        "recent_traders_seed discovered=%d added=%d active=%d",
        len(wallets), added, active,
    )
    return {
        "recent_traders_discovered": len(wallets),
        "added": added,
        "active_wallets": active,
    }


def run_prune_wallets(
    *,
    dormant_days: int = DEEP_DEFAULT_DORMANT_DAYS,
    dry_run: bool = False,
) -> dict[str, int]:
    """Standalone prune: read existing review-state, apply protection rules,
    write back. Used by the CLI `prune-wallets` and `POST /ingestor/prune-wallets`.

    Treats the empty `discovered` set as "no fresh sweep available, only
    archive on dormancy". Caller can run a sweep first if they want sweep-
    backed pruning.
    """
    review_path = _review_state_path()
    state = _load_review_state(review_path)
    data_dir = _data_dir()
    pruned = prune_review_state(
        state=state,
        discovered=set(),
        enrichment_path=data_dir / "polymarket_wallet_enrichment.jsonl",
        positions_path=data_dir / "polymarket_positions.jsonl",
        dormant_days=dormant_days,
        dry_run=dry_run,
    )
    if not dry_run:
        _write_review_state(review_path, state)
    active = sum(1 for r in state.values() if r.status == "active")
    archived = sum(1 for r in state.values() if r.status == "archived")
    pinned = sum(1 for r in state.values() if r.status == "pinned")
    return {
        "pruned_this_run": pruned,
        "active_wallets": active,
        "archived_wallets": archived,
        "pinned_wallets": pinned,
    }


def run_pin_wallet(address: str, *, pin: bool) -> str:
    """Set status=pinned (or revert to active) for a wallet. Creates the
    review-state row if it doesn't exist. Returns the resulting status."""
    review_path = _review_state_path()
    state = _load_review_state(review_path)
    wallet = address.strip().lower()
    if not wallet:
        raise ValueError("address must be non-empty")
    record = state.get(wallet)
    target_status = "pinned" if pin else "active"
    if record is None:
        record = PolymarketWalletReviewState(
            proxy_wallet=wallet,
            status=target_status,
            first_seen_at=_utcnow_iso(),
        )
        state[wallet] = record
    else:
        record.status = target_status
        if pin:
            record.archived_at = None
            record.archived_reason = None
    _write_review_state(review_path, state)
    log.info("pin_wallet wallet=%s status=%s", wallet, target_status)
    return target_status


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
                log.error(
                    "no wallets to ingest — run 'seed-watchlist' first, pass --address, "
                    "or populate %s",
                    wl_path,
                )
                return 1
            run_wallets(
                client,
                stores,
                addresses=addresses,
                activity_page_size=args.activity_page_size,
                max_pages_per_wallet=args.max_pages_per_wallet,
                full_backfill=args.full_backfill,
            )
        elif args.mode == "seed-watchlist":
            seed_watchlist_from_leaderboard(
                client,
                stores,
                _watchlist_path(),
                top_n_profit=args.top_n_profit,
                top_n_volume=args.top_n_volume,
            )
        elif args.mode == "markets":
            closed: bool | None = None
            if args.closed:
                closed = True
            elif args.active:
                closed = False
            run_markets(
                client, stores, closed=closed, pages=args.pages, page_size=args.page_size,
                order=args.order, ascending=args.ascending,
            )
            if args.backfill_from_activity:
                run_markets_backfill_from_activity(client, stores)
        elif args.mode == "enrichment":
            run_enrichment(stores)
        elif args.mode == "fetch-kalshi-markets":
            run_fetch_kalshi_markets(stores, status=args.status, max_pages=args.max_pages)
        elif args.mode == "match-markets":
            cfg = MatchConfig(
                date_window_days=args.date_window_days,
                auto_approve_threshold=args.auto_approve_threshold,
                review_threshold=args.review_threshold,
                max_per_kalshi=args.max_per_kalshi,
            )
            run_match_markets(stores, config=cfg)
        elif args.mode == "review-matches":
            run_review_matches(stores, limit=args.limit)
        elif args.mode == "pipeline":
            windows = args.window or None
            run_pipeline(
                windows=windows,
                leaderboard_limit=args.leaderboard_limit,
                skip_kalshi=args.skip_kalshi,
                include_profit_leaderboard=args.include_profit,
                client=client,
            )
        elif args.mode == "deep-pipeline":
            run_deep_pipeline(
                leaderboard_depth=args.leaderboard_depth,
                wallet_batch_size=args.wallet_batch_size,
                activity_pages=args.activity_pages,
                dormant_days=args.dormant_days,
                seed_recent_traders=not args.no_recent_traders,
                recent_trader_limit=args.recent_trader_limit,
                skip_kalshi=args.skip_kalshi,
                client=client,
            )
        elif args.mode == "recent-traders":
            counts = run_recent_traders_seed(
                limit=args.limit, max_pages=args.pages, client=client,
            )
            log.info("recent_traders %s", counts)
        elif args.mode == "prune-wallets":
            counts = run_prune_wallets(
                dormant_days=args.dormant_days, dry_run=args.dry_run,
            )
            log.info("prune_wallets %s", counts)
        elif args.mode == "pin-wallet":
            run_pin_wallet(args.address, pin=True)
        elif args.mode == "unpin-wallet":
            run_pin_wallet(args.address, pin=False)
        elif args.mode == "all":
            if os.getenv("INGEST_POLYMARKET", "").strip().lower() not in {"1", "true", "yes", "on"}:
                log.info("INGEST_POLYMARKET not set — skipping 'all' pass (set to '1' to enable)")
                return 0
            # Profit dropped (top_n_profit=0): the PnL leaderboard over-weights
            # lucky single-win wallets. Volume only.
            addresses = seed_watchlist_from_leaderboard(
                client, stores, _watchlist_path(), top_n_profit=0, top_n_volume=50
            )
            if addresses:
                run_wallets(
                    client,
                    stores,
                    addresses=addresses,
                    activity_page_size=500,
                    max_pages_per_wallet=20,
                )
            run_markets(client, stores, closed=True, pages=5, page_size=100)
            # Backfill any condition_ids that wallets traded on but we haven't fetched yet —
            # this is what makes skill scoring actually work for top-trader wallets.
            run_markets_backfill_from_activity(client, stores)
            run_enrichment(stores)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
