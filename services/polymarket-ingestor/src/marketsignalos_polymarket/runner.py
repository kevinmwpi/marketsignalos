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
from dataclasses import dataclass
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
) -> tuple[int, int, int]:
    """
    Returns (activity_written, positions_written, values_written).

    full_backfill=True ignores the per-wallet checkpoint and walks every page
    up to max_pages. Default (False) only fetches events newer than the last
    seen timestamp — the steady-state path.
    """
    total_activity = total_positions = total_values = 0
    for addr in addresses:
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


# ── Cross-exchange matching ──────────────────────────────────────────────────

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

    sd = sub.add_parser("seed-watchlist", help="Seed the watchlist from profit + volume leaderboards")
    sd.add_argument("--top-n-profit", type=int, default=50)
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

    mm = sub.add_parser("match-markets", help="Run cross-exchange title matcher")
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
    return parser


@dataclass(slots=True)
class PipelineResult:
    """Counts surfaced back to the API so the UI can summarize the run."""

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
    client: PolymarketClient | None = None,
) -> PipelineResult:
    """End-to-end Polymarket pipeline:

        1. Seed the wallet watchlist by hitting profit + volume leaderboards
           across each configured time window. Unsupported windows are
           skipped with a warning (the Polymarket public leaderboard API
           silently rejects some window values).
        2. Pull each wallet's recent activity, current positions, and value.
        3. Fetch resolved-market metadata (closed=True) for skill scoring,
           plus backfill any condition_ids referenced by activity that
           weren't in the page set.
        4. Recompute per-wallet enrichment (win rate, skill likelihood).
        5. Fetch Kalshi's public /markets and run the cross-exchange title
           matcher to populate market_links.jsonl — the join the
           /signals/skilled-bets and /signals/cross-exchange endpoints
           use to surface "mirror this on Kalshi" links.

    All public APIs hit here are unauthenticated, so this function needs
    zero env-var configuration to run.
    """
    attempts = list(windows or _DEFAULT_WINDOWS)
    stores = _build_stores(_data_dir())
    owns_client = client is None
    client = client or PolymarketClient(PolymarketClientConfig.from_env())
    try:
        # 1. Seed watchlist across (window × metric)
        log.info("pipeline step=seed_watchlist windows=%s", attempts)
        existing = set(_load_watchlist(_watchlist_path()))
        seeded: set[str] = set(existing)
        leaderboard_entries = 0
        succeeded: list[str] = []
        for window in attempts:
            window_ok = False
            for metric in ("profit", "volume"):
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
            activity_total, positions_total, values_total = run_wallets(
                client,
                stores,
                addresses=merged,
                activity_page_size=activity_page_size,
                max_pages_per_wallet=max_pages_per_wallet,
            )

        # 3. Polymarket market metadata + activity-backfill
        log.info("pipeline step=markets")
        markets_written = run_markets(
            client, stores,
            closed=True, pages=market_pages, page_size=market_page_size,
        )
        markets_backfilled = run_markets_backfill_from_activity(client, stores)

        # 4. Compute on-chain skill enrichment
        log.info("pipeline step=enrichment")
        enrichment_written = run_enrichment(stores)

        # 5. Kalshi mirror: fetch public markets + match
        kalshi_written = 0
        links_written = 0
        if not skip_kalshi:
            log.info("pipeline step=kalshi_markets status=%s", kalshi_status)
            try:
                kalshi_written = run_fetch_kalshi_markets(
                    stores, status=kalshi_status, max_pages=kalshi_max_pages,
                )
                log.info("pipeline step=match_markets")
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
                client=client,
            )
        elif args.mode == "all":
            if os.getenv("INGEST_POLYMARKET", "").strip().lower() not in {"1", "true", "yes", "on"}:
                log.info("INGEST_POLYMARKET not set — skipping 'all' pass (set to '1' to enable)")
                return 0
            addresses = seed_watchlist_from_leaderboard(
                client, stores, _watchlist_path(), top_n_profit=50, top_n_volume=50
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
