"""
JSONL stores for Polymarket data.

Pattern follows services/ingestor/storage.py:
  - One protocol per record type
  - Append-only JSONL writes
  - Dedupe via sidecar index file where the record stream is unbounded
  - Postgres stores are stubbed until the schema lands

Dedupe keys:
  - Activity:  transaction_hash + condition_id + outcome_index  (one wallet can
               have multiple positions on same condition_id at different outcomes)
  - Markets:   condition_id
  - Positions: NOT deduped — each fetch is a fresh snapshot
  - Leaderboard snapshots: NOT deduped — historical series
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .models import (
    MarketLink,
    PolymarketActivity,
    PolymarketClosedPosition,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketPositionSnapshot,
    PolymarketPriceSnapshot,
    PolymarketWalletBet,
    PolymarketWalletEnrichment,
    PolymarketWalletHydration,
    PolymarketWalletValue,
)


# ── Protocols ─────────────────────────────────────────────────────────────────

class LeaderboardStore(Protocol):
    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int: ...


class ActivityStore(Protocol):
    def write_activity(self, events: list[PolymarketActivity]) -> int: ...
    def flush(self) -> None: ...


class PositionStore(Protocol):
    def write_positions(self, positions: list[PolymarketPosition]) -> int: ...


class PositionSnapshotStore(Protocol):
    def write_position_snapshot(self, snapshot: PolymarketPositionSnapshot) -> int: ...


class ClosedPositionStore(Protocol):
    def write_closed_positions(self, positions: list[PolymarketClosedPosition]) -> int: ...


class WalletHydrationStore(Protocol):
    def load_hydration(self) -> dict[str, PolymarketWalletHydration]: ...
    def upsert_hydration(self, rows: list[PolymarketWalletHydration]) -> int: ...


class MarketStore(Protocol):
    def write_markets(self, markets: list[PolymarketMarket]) -> int: ...


class WalletValueStore(Protocol):
    def write_values(self, values: list[PolymarketWalletValue]) -> int: ...


class WalletCheckpointStore(Protocol):
    def get_last_timestamp(self, wallet: str) -> int | None: ...
    def set_last_timestamp(self, wallet: str, timestamp: int) -> None: ...


class EnrichmentStore(Protocol):
    def write_enrichment(self, enrichments: list[PolymarketWalletEnrichment]) -> int: ...


class WalletBetStore(Protocol):
    def write_bets(self, bets: list[PolymarketWalletBet]) -> int: ...


class PriceSnapshotStore(Protocol):
    def write_snapshots(self, snapshots: list[PolymarketPriceSnapshot]) -> int: ...


class MarketLinkStore(Protocol):
    def load_links(self) -> list[MarketLink]: ...
    def upsert_links(self, links: list[MarketLink]) -> int: ...


# ── JSONL implementations ─────────────────────────────────────────────────────

def _read_index(index_path: Path) -> set[str]:
    if not index_path.exists():
        return set()
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{index_path} must contain a JSON array")
    return {value for value in raw if isinstance(value, str)}


def _write_index(index_path: Path, values: set[str]) -> None:
    # Compact (no indent) + atomic temp-swap. The index can reach multiple GB;
    # pretty-printing roughly doubles its size for zero benefit, and a partial
    # write here would corrupt cross-run dedupe.
    tmp = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(values), separators=(",", ":")), encoding="utf-8")
    tmp.replace(index_path)


class JsonlLeaderboardStore:
    """Append-only — every snapshot is preserved for historical analysis."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int:
        if not entries:
            return 0
        with self._path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        return len(entries)


# Fields persisted to the activity JSONL. price/outcome/slug/title are dropped
# on purpose: skill computation never reads them and they are derivable from
# polymarket_markets.jsonl by condition_id. They stay on the in-memory dataclass
# so the Postgres dual-write path is unaffected. transaction_hash is kept so the
# dedupe index can be rebuilt from the file if it is ever lost.
_ACTIVITY_JSONL_FIELDS = (
    "proxy_wallet",
    "timestamp",
    "condition_id",
    "type",
    "side",
    "size",
    "usdc_size",
    "outcome_index",
    "event_slug",
    "transaction_hash",
    "name",
    "pseudonym",
    "fetched_at",
)


def _dump_activity(event: PolymarketActivity) -> str:
    row = asdict(event)
    return json.dumps(
        {key: row[key] for key in _ACTIVITY_JSONL_FIELDS}, separators=(",", ":")
    )


class JsonlActivityStore:
    """Append + dedupe on (transaction_hash, condition_id, outcome_index, type).

    The dedupe index is loaded from disk once, held in memory for the life of
    the store, and persisted with flush(). Re-reading and re-writing the
    (potentially multi-GB) sidecar index on every per-wallet write was O(n) in
    the index size and dominated ingest time as history grew. Callers must
    flush() once the activity write loop is done (see runner.run_wallets).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path = path.with_suffix(path.suffix + ".index.json")
        self._index: set[str] | None = None
        self._index_dirty = False

    def _ensure_index(self) -> set[str]:
        if self._index is None:
            self._index = _read_index(self._index_path)
        return self._index

    def write_activity(self, events: list[PolymarketActivity]) -> int:
        if not events:
            return 0
        index = self._ensure_index()
        written = 0
        with self._path.open("a", encoding="utf-8") as fh:
            for event in events:
                key = self._dedupe_key(event)
                if key in index:
                    continue
                index.add(key)
                fh.write(_dump_activity(event) + "\n")
                written += 1
        if written:
            self._index_dirty = True
        return written

    def flush(self) -> None:
        if self._index is not None and self._index_dirty:
            _write_index(self._index_path, self._index)
            self._index_dirty = False

    @staticmethod
    def _dedupe_key(event: PolymarketActivity) -> str:
        # transaction_hash alone isn't unique — one tx can settle multiple legs.
        return f"{event.transaction_hash}:{event.condition_id}:{event.outcome_index}:{event.type}"


class JsonlPositionStore:
    """Snapshot semantics: each call appends a fresh snapshot with snapshot_at."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_positions(self, positions: list[PolymarketPosition]) -> int:
        if not positions:
            return 0
        with self._path.open("a", encoding="utf-8") as fh:
            for pos in positions:
                fh.write(json.dumps(asdict(pos), separators=(",", ":")) + "\n")
        return len(positions)


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def _atomic_write_rows(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(asdict(row), separators=(",", ":")) + "\n")
    tmp.replace(path)


class JsonlPositionSnapshotStore:
    """Latest complete snapshot manifest per wallet."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_position_snapshot(self, snapshot: PolymarketPositionSnapshot) -> int:
        rows: dict[str, PolymarketPositionSnapshot] = {}
        for raw in _read_jsonl_rows(self._path):
            try:
                current = PolymarketPositionSnapshot(**raw)
                rows[current.proxy_wallet.lower()] = current
            except TypeError:
                continue
        rows[snapshot.proxy_wallet.lower()] = snapshot
        _atomic_write_rows(self._path, [rows[key] for key in sorted(rows)])
        return 1


class JsonlClosedPositionStore:
    """Latest audited /closed-positions row per wallet leg."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_closed_positions(self, positions: list[PolymarketClosedPosition]) -> int:
        if not positions:
            return 0
        rows: dict[tuple[str, str, int], PolymarketClosedPosition] = {}
        for raw in _read_jsonl_rows(self._path):
            try:
                current = PolymarketClosedPosition(**raw)
                key = (
                    current.proxy_wallet.lower(),
                    current.condition_id,
                    current.outcome_index,
                )
                rows[key] = current
            except TypeError:
                continue
        for pos in positions:
            rows[(pos.proxy_wallet.lower(), pos.condition_id, pos.outcome_index)] = pos
        _atomic_write_rows(self._path, [rows[key] for key in sorted(rows)])
        return len(positions)


class JsonlWalletHydrationStore:
    """Atomic latest hydration state keyed by wallet."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_hydration(self) -> dict[str, PolymarketWalletHydration]:
        rows: dict[str, PolymarketWalletHydration] = {}
        for raw in _read_jsonl_rows(self._path):
            try:
                item = PolymarketWalletHydration(**raw)
            except TypeError:
                continue
            rows[item.proxy_wallet.lower()] = item
        return rows

    def upsert_hydration(self, rows: list[PolymarketWalletHydration]) -> int:
        merged = self.load_hydration()
        for row in rows:
            merged[row.proxy_wallet.lower()] = row
        _atomic_write_rows(self._path, [merged[key] for key in sorted(merged)])
        return len(rows)


class JsonlMarketStore:
    """Append + dedupe on condition_id. Closed-market re-fetches overwrite by adding
    a new row; downstream readers should keep the most recent fetched_at."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_markets(self, markets: list[PolymarketMarket]) -> int:
        if not markets:
            return 0
        # We DO want to refresh closed/active status, so dedupe is by
        # (condition_id, closed_bool) — lets us record the transition.
        latest: dict[str, PolymarketMarket] = {}
        for raw in _read_jsonl_rows(self._path):
            try:
                current = PolymarketMarket(**raw)
                latest[current.condition_id] = current
            except TypeError:
                continue
        for market in markets:
            latest[market.condition_id] = market
        _atomic_write_rows(self._path, [latest[key] for key in sorted(latest)])
        return len(markets)


class JsonlWalletValueStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_values(self, values: list[PolymarketWalletValue]) -> int:
        if not values:
            return 0
        with self._path.open("a", encoding="utf-8") as fh:
            for v in values:
                fh.write(json.dumps(asdict(v), separators=(",", ":")) + "\n")
        return len(values)


class JsonlMarketLinkStore:
    """
    Keyed by (kalshi_ticker, polymarket_condition_id). On upsert, MANUAL
    decisions are sticky — a re-run of the auto matcher cannot overwrite
    a human rejection or approval. This is what lets us silence repeated
    false positives.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_links(self) -> list[MarketLink]:
        if not self._path.exists():
            return []
        out: list[MarketLink] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(MarketLink(**obj))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def upsert_links(self, links: list[MarketLink]) -> int:
        existing = self.load_links()
        by_key: dict[tuple[str, str], MarketLink] = {
            (link.kalshi_ticker, link.polymarket_condition_id): link
            for link in existing
        }
        written = 0
        for incoming in links:
            key = (incoming.kalshi_ticker, incoming.polymarket_condition_id)
            current = by_key.get(key)
            if current is not None and current.matched_by == "manual":
                # Human decision wins — never overwrite.
                continue
            by_key[key] = incoming
            written += 1
        with self._path.open("w", encoding="utf-8") as fh:
            for link in by_key.values():
                fh.write(json.dumps(asdict(link), separators=(",", ":")) + "\n")
        return written


class JsonlEnrichmentStore:
    """Overwrite semantics: each computation pass replaces the whole file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_enrichment(self, enrichments: list[PolymarketWalletEnrichment]) -> int:
        # Truncate + rewrite — enrichment is always recomputed from scratch.
        with self._path.open("w", encoding="utf-8") as fh:
            for e in enrichments:
                fh.write(json.dumps(asdict(e), separators=(",", ":")) + "\n")
        return len(enrichments)


class JsonlWalletBetStore:
    """Overwrite semantics: bet records are recomputed wholesale per pass."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_bets(self, bets: list[PolymarketWalletBet]) -> int:
        with self._path.open("w", encoding="utf-8") as fh:
            for bet in bets:
                fh.write(json.dumps(asdict(bet), separators=(",", ":")) + "\n")
        return len(bets)


class JsonlPriceSnapshotStore:
    """
    Append-only price history, deduped on (condition_id, fetched_at) so a
    market fetched on overlapping pages within one run records one point.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[tuple[str, str]] = {
            (str(row.get("condition_id", "")), str(row.get("fetched_at", "")))
            for row in _read_jsonl_rows(self._path)
        }

    def write_snapshots(self, snapshots: list[PolymarketPriceSnapshot]) -> int:
        written = 0
        with self._path.open("a", encoding="utf-8") as fh:
            for snap in snapshots:
                key = (snap.condition_id, snap.fetched_at)
                if not snap.condition_id or key in self._seen:
                    continue
                self._seen.add(key)
                fh.write(json.dumps(asdict(snap), separators=(",", ":")) + "\n")
                written += 1
        return written


class JsonWalletCheckpointStore:
    """
    Per-wallet checkpoint: last seen activity timestamp.

    Re-running ingestion only pulls activity newer than this watermark, so
    backfills are bounded and steady-state runs stay cheap.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, int] = self._read()

    def get_last_timestamp(self, wallet: str) -> int | None:
        return self._state.get(wallet.lower())

    def set_last_timestamp(self, wallet: str, timestamp: int) -> None:
        prior = self._state.get(wallet.lower())
        if prior is None or timestamp > prior:
            self._state[wallet.lower()] = timestamp
            self._write()

    def _read(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{self._path} must contain a JSON object")
        return {str(k).lower(): int(v) for k, v in raw.items() if isinstance(v, (int, float))}

    def _write(self) -> None:
        self._path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )


# ── Postgres implementations (Phase 7.5) ──────────────────────────────────────
#
# Writes-only path: ingestor persists to Postgres when DATABASE_URL is set,
# API still reads from JSONL. Connections are short-lived per write call to
# match the Kalshi pattern — no shared pool yet.

def _pg_connect(dsn: str):  # type: ignore[no-untyped-def]
    """Lazy psycopg2 import so JSONL-only test paths don't need the driver."""
    import psycopg2
    return psycopg2.connect(dsn)


class PostgresLeaderboardStore:
    """Append-only snapshots; PK (proxy_wallet, metric, window, fetched_at)
    means re-running the same snapshot is a no-op via ON CONFLICT DO NOTHING."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int:
        if not entries:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_leaderboard
                        (proxy_wallet, name, pseudonym, amount_usdc,
                         metric, window_label, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet, metric, window_label, fetched_at) DO NOTHING
                    """,
                    [
                        (e.proxy_wallet, e.name, e.pseudonym, e.amount_usdc,
                         e.metric, e.window, e.fetched_at)
                        for e in entries
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(entries)


class PostgresActivityStore:
    """One row per (tx_hash, condition_id, outcome_index, type). Activity rows
    are immutable on-chain, so duplicates are silently skipped. The model's
    `timestamp` (unix seconds) is materialized into `traded_at TIMESTAMPTZ`."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_activity(self, events: list[PolymarketActivity]) -> int:
        if not events:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_activity
                        (proxy_wallet, transaction_hash, condition_id,
                         outcome_index, type, side, size, usdc_size, price,
                         outcome, slug, title, event_slug, name, pseudonym,
                         traded_at, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            to_timestamp(%s), %s::timestamptz)
                    ON CONFLICT (transaction_hash, condition_id, outcome_index, type)
                        DO NOTHING
                    """,
                    [
                        (e.proxy_wallet, e.transaction_hash, e.condition_id,
                         e.outcome_index, e.type, e.side, e.size, e.usdc_size, e.price,
                         e.outcome, e.slug, e.title, e.event_slug, e.name, e.pseudonym,
                         e.timestamp, e.fetched_at)
                        for e in events
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(events)

    def flush(self) -> None:
        # Postgres writes commit per call; nothing is buffered.
        return None


class PostgresPositionStore:
    """Snapshot semantics — PK includes snapshot_at so each fetch persists.
    Same-instant duplicates (rare) collapse via ON CONFLICT DO NOTHING."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_positions(self, positions: list[PolymarketPosition]) -> int:
        if not positions:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_positions
                        (proxy_wallet, condition_id, outcome_index, outcome,
                         size, avg_price, current_value_usdc, current_outcome_price,
                         cash_pnl_usdc, slug, title, event_slug, snapshot_id, snapshot_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::timestamptz)
                    ON CONFLICT (proxy_wallet, condition_id, outcome_index, snapshot_at)
                        DO NOTHING
                    """,
                    [
                        (p.proxy_wallet, p.condition_id, p.outcome_index, p.outcome,
                         p.size, p.avg_price, p.current_value_usdc,
                         p.current_outcome_price, p.cash_pnl_usdc,
                         p.slug, p.title, p.event_slug, p.snapshot_id, p.snapshot_at)
                        for p in positions
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(positions)


class PostgresPositionSnapshotStore:
    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_position_snapshot(self, snapshot: PolymarketPositionSnapshot) -> int:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO polymarket_position_snapshots
                        (proxy_wallet, snapshot_id, position_count, complete, snapshot_at)
                    VALUES (%s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet) DO UPDATE SET
                        snapshot_id = EXCLUDED.snapshot_id,
                        position_count = EXCLUDED.position_count,
                        complete = EXCLUDED.complete,
                        snapshot_at = EXCLUDED.snapshot_at
                    """,
                    (
                        snapshot.proxy_wallet, snapshot.snapshot_id,
                        snapshot.position_count, snapshot.complete, snapshot.snapshot_at,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return 1


class PostgresClosedPositionStore:
    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_closed_positions(self, positions: list[PolymarketClosedPosition]) -> int:
        if not positions:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_closed_positions
                        (proxy_wallet, condition_id, outcome_index, outcome, size,
                         avg_price, realized_pnl_usdc, current_outcome_price,
                         slug, title, event_slug, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet, condition_id, outcome_index) DO UPDATE SET
                        outcome = EXCLUDED.outcome,
                        size = EXCLUDED.size,
                        avg_price = EXCLUDED.avg_price,
                        realized_pnl_usdc = EXCLUDED.realized_pnl_usdc,
                        current_outcome_price = EXCLUDED.current_outcome_price,
                        slug = EXCLUDED.slug,
                        title = EXCLUDED.title,
                        event_slug = EXCLUDED.event_slug,
                        fetched_at = EXCLUDED.fetched_at
                    """,
                    [
                        (
                            p.proxy_wallet, p.condition_id, p.outcome_index, p.outcome,
                            p.size, p.avg_price, p.realized_pnl_usdc,
                            p.current_outcome_price, p.slug, p.title, p.event_slug,
                            p.fetched_at,
                        )
                        for p in positions
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(positions)


class PostgresWalletHydrationStore:
    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def load_hydration(self) -> dict[str, PolymarketWalletHydration]:
        return {}

    def upsert_hydration(self, rows: list[PolymarketWalletHydration]) -> int:
        if not rows:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_wallet_hydration
                        (proxy_wallet, newest_activity_timestamp,
                         oldest_activity_cursor_timestamp, activity_history_complete,
                         positions_complete, closed_positions_complete,
                         economic_all_time_complete, economic_month_complete,
                         metadata_condition_count, metadata_covered_count,
                         metadata_coverage, all_time_pnl_usdc, all_time_volume_usdc,
                         pnl_30d_usdc, active_pnl_usdc, max_drawdown_usdc,
                         latest_position_snapshot_id, last_refreshed_at, errors)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s::timestamptz, %s::jsonb)
                    ON CONFLICT (proxy_wallet) DO UPDATE SET
                        newest_activity_timestamp = EXCLUDED.newest_activity_timestamp,
                        oldest_activity_cursor_timestamp = EXCLUDED.oldest_activity_cursor_timestamp,
                        activity_history_complete = EXCLUDED.activity_history_complete,
                        positions_complete = EXCLUDED.positions_complete,
                        closed_positions_complete = EXCLUDED.closed_positions_complete,
                        economic_all_time_complete = EXCLUDED.economic_all_time_complete,
                        economic_month_complete = EXCLUDED.economic_month_complete,
                        metadata_condition_count = EXCLUDED.metadata_condition_count,
                        metadata_covered_count = EXCLUDED.metadata_covered_count,
                        metadata_coverage = EXCLUDED.metadata_coverage,
                        all_time_pnl_usdc = EXCLUDED.all_time_pnl_usdc,
                        all_time_volume_usdc = EXCLUDED.all_time_volume_usdc,
                        pnl_30d_usdc = EXCLUDED.pnl_30d_usdc,
                        active_pnl_usdc = EXCLUDED.active_pnl_usdc,
                        max_drawdown_usdc = EXCLUDED.max_drawdown_usdc,
                        latest_position_snapshot_id = EXCLUDED.latest_position_snapshot_id,
                        last_refreshed_at = EXCLUDED.last_refreshed_at,
                        errors = EXCLUDED.errors
                    """,
                    [
                        (
                            r.proxy_wallet, r.newest_activity_timestamp,
                            r.oldest_activity_cursor_timestamp,
                            r.activity_history_complete, r.positions_complete,
                            r.closed_positions_complete, r.economic_all_time_complete,
                            r.economic_month_complete, r.metadata_condition_count,
                            r.metadata_covered_count, r.metadata_coverage,
                            r.all_time_pnl_usdc, r.all_time_volume_usdc,
                            r.pnl_30d_usdc, r.active_pnl_usdc, r.max_drawdown_usdc,
                            r.latest_position_snapshot_id, r.last_refreshed_at,
                            json.dumps(r.errors),
                        )
                        for r in rows
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(rows)


class PostgresMarketStore:
    """Reference data — upsert by condition_id, always overwrite to the
    freshest fetched_at. outcomes / outcome_prices are stored as JSONB."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_markets(self, markets: list[PolymarketMarket]) -> int:
        if not markets:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_markets
                        (condition_id, gamma_id, slug, question, category,
                         end_date, outcomes, outcome_prices, volume_usdc,
                         liquidity_usdc, closed, active, last_trade_price,
                         best_bid, best_ask, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                            %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (condition_id) DO UPDATE SET
                        gamma_id         = EXCLUDED.gamma_id,
                        slug             = EXCLUDED.slug,
                        question         = EXCLUDED.question,
                        category         = EXCLUDED.category,
                        end_date         = EXCLUDED.end_date,
                        outcomes         = EXCLUDED.outcomes,
                        outcome_prices   = EXCLUDED.outcome_prices,
                        volume_usdc      = EXCLUDED.volume_usdc,
                        liquidity_usdc   = EXCLUDED.liquidity_usdc,
                        closed           = EXCLUDED.closed,
                        active           = EXCLUDED.active,
                        last_trade_price = EXCLUDED.last_trade_price,
                        best_bid         = EXCLUDED.best_bid,
                        best_ask         = EXCLUDED.best_ask,
                        fetched_at       = EXCLUDED.fetched_at
                    """,
                    [
                        (m.condition_id, m.gamma_id, m.slug, m.question, m.category,
                         m.end_date, json.dumps(m.outcomes), json.dumps(m.outcome_prices),
                         m.volume_usdc, m.liquidity_usdc, m.closed, m.active,
                         m.last_trade_price, m.best_bid, m.best_ask, m.fetched_at)
                        for m in markets
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(markets)


class PostgresWalletValueStore:
    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_values(self, values: list[PolymarketWalletValue]) -> int:
        if not values:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_wallet_values
                        (proxy_wallet, value_usdc, snapshot_at)
                    VALUES (%s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet, snapshot_at) DO NOTHING
                    """,
                    [(v.proxy_wallet, v.value_usdc, v.snapshot_at) for v in values],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(values)


class PostgresEnrichmentStore:
    """One row per wallet. Each pass overwrites all fields including computed_at."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_enrichment(self, enrichments: list[PolymarketWalletEnrichment]) -> int:
        if not enrichments:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_wallet_enrichment
                        (proxy_wallet, name, pseudonym, resolved_trades, wins, losses,
                         win_rate, skill_likelihood, stddevs_above_expected,
                         edge_mean, edge_lower_bound, effective_sample_size,
                         resolved_volume_usdc, rank_score,
                         total_volume_usdc, total_pnl_usdc, avg_position_size_usdc,
                         trade_count, last_activity_at,
                         forecast_skill_likelihood, forecast_edge_mean,
                         forecast_edge_lower_bound, independent_settled_events,
                         all_time_pnl_usdc, all_time_volume_usdc, all_time_roi,
                         pnl_30d_usdc, active_pnl_usdc, max_drawdown_usdc,
                         recent_skill_likelihood, recent_edge_mean,
                         recent_edge_lower_bound, recent_independent_events,
                         clv_mean, clv_lower_bound, clv_sample_size,
                         category_edges,
                         price_lead_score, price_lead_drivers,
                         post_entry_drift_48h, post_entry_drift_samples,
                         longshot_win_rate, longshot_implied_rate, longshot_events,
                         late_entry_win_rate, late_entry_implied_rate,
                         late_entry_events,
                         style_archetype, automation_score, style_drivers,
                         trades_per_active_day, median_intertrade_gap_seconds,
                         active_utc_hours, buy_size_uniformity,
                         markets_per_active_day, top_category,
                         top_category_share, exited_position_share,
                         median_exit_hold_hours,
                         data_quality_status, data_quality_reasons,
                         economic_qualified, tailability_status,
                         tailability_reasons, score_version, computed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s::jsonb,
                            %s, %s::jsonb,
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s::jsonb,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet) DO UPDATE SET
                        name                   = EXCLUDED.name,
                        pseudonym              = EXCLUDED.pseudonym,
                        resolved_trades        = EXCLUDED.resolved_trades,
                        wins                   = EXCLUDED.wins,
                        losses                 = EXCLUDED.losses,
                        win_rate               = EXCLUDED.win_rate,
                        skill_likelihood       = EXCLUDED.skill_likelihood,
                        stddevs_above_expected = EXCLUDED.stddevs_above_expected,
                        edge_mean              = EXCLUDED.edge_mean,
                        edge_lower_bound       = EXCLUDED.edge_lower_bound,
                        effective_sample_size  = EXCLUDED.effective_sample_size,
                        resolved_volume_usdc   = EXCLUDED.resolved_volume_usdc,
                        rank_score             = EXCLUDED.rank_score,
                        total_volume_usdc      = EXCLUDED.total_volume_usdc,
                        total_pnl_usdc         = EXCLUDED.total_pnl_usdc,
                        avg_position_size_usdc = EXCLUDED.avg_position_size_usdc,
                        trade_count            = EXCLUDED.trade_count,
                        last_activity_at       = EXCLUDED.last_activity_at,
                        forecast_skill_likelihood = EXCLUDED.forecast_skill_likelihood,
                        forecast_edge_mean = EXCLUDED.forecast_edge_mean,
                        forecast_edge_lower_bound = EXCLUDED.forecast_edge_lower_bound,
                        independent_settled_events = EXCLUDED.independent_settled_events,
                        all_time_pnl_usdc = EXCLUDED.all_time_pnl_usdc,
                        all_time_volume_usdc = EXCLUDED.all_time_volume_usdc,
                        all_time_roi = EXCLUDED.all_time_roi,
                        pnl_30d_usdc = EXCLUDED.pnl_30d_usdc,
                        active_pnl_usdc = EXCLUDED.active_pnl_usdc,
                        max_drawdown_usdc = EXCLUDED.max_drawdown_usdc,
                        recent_skill_likelihood = EXCLUDED.recent_skill_likelihood,
                        recent_edge_mean = EXCLUDED.recent_edge_mean,
                        recent_edge_lower_bound = EXCLUDED.recent_edge_lower_bound,
                        recent_independent_events = EXCLUDED.recent_independent_events,
                        clv_mean = EXCLUDED.clv_mean,
                        clv_lower_bound = EXCLUDED.clv_lower_bound,
                        clv_sample_size = EXCLUDED.clv_sample_size,
                        category_edges = EXCLUDED.category_edges,
                        price_lead_score = EXCLUDED.price_lead_score,
                        price_lead_drivers = EXCLUDED.price_lead_drivers,
                        post_entry_drift_48h = EXCLUDED.post_entry_drift_48h,
                        post_entry_drift_samples = EXCLUDED.post_entry_drift_samples,
                        longshot_win_rate = EXCLUDED.longshot_win_rate,
                        longshot_implied_rate = EXCLUDED.longshot_implied_rate,
                        longshot_events = EXCLUDED.longshot_events,
                        late_entry_win_rate = EXCLUDED.late_entry_win_rate,
                        late_entry_implied_rate = EXCLUDED.late_entry_implied_rate,
                        late_entry_events = EXCLUDED.late_entry_events,
                        style_archetype = EXCLUDED.style_archetype,
                        automation_score = EXCLUDED.automation_score,
                        style_drivers = EXCLUDED.style_drivers,
                        trades_per_active_day = EXCLUDED.trades_per_active_day,
                        median_intertrade_gap_seconds = EXCLUDED.median_intertrade_gap_seconds,
                        active_utc_hours = EXCLUDED.active_utc_hours,
                        buy_size_uniformity = EXCLUDED.buy_size_uniformity,
                        markets_per_active_day = EXCLUDED.markets_per_active_day,
                        top_category = EXCLUDED.top_category,
                        top_category_share = EXCLUDED.top_category_share,
                        exited_position_share = EXCLUDED.exited_position_share,
                        median_exit_hold_hours = EXCLUDED.median_exit_hold_hours,
                        data_quality_status = EXCLUDED.data_quality_status,
                        data_quality_reasons = EXCLUDED.data_quality_reasons,
                        economic_qualified = EXCLUDED.economic_qualified,
                        tailability_status = EXCLUDED.tailability_status,
                        tailability_reasons = EXCLUDED.tailability_reasons,
                        score_version = EXCLUDED.score_version,
                        computed_at            = EXCLUDED.computed_at
                    """,
                    [
                        (e.proxy_wallet, e.name, e.pseudonym, e.resolved_trades,
                         e.wins, e.losses, e.win_rate, e.skill_likelihood,
                         e.stddevs_above_expected,
                         e.edge_mean, e.edge_lower_bound, e.effective_sample_size,
                         e.resolved_volume_usdc, e.rank_score,
                         e.total_volume_usdc,
                         e.total_pnl_usdc, e.avg_position_size_usdc,
                         e.trade_count, e.last_activity_at,
                         e.forecast_skill_likelihood, e.forecast_edge_mean,
                         e.forecast_edge_lower_bound, e.independent_settled_events,
                         e.all_time_pnl_usdc, e.all_time_volume_usdc, e.all_time_roi,
                         e.pnl_30d_usdc, e.active_pnl_usdc, e.max_drawdown_usdc,
                         e.recent_skill_likelihood, e.recent_edge_mean,
                         e.recent_edge_lower_bound, e.recent_independent_events,
                         e.clv_mean, e.clv_lower_bound, e.clv_sample_size,
                         json.dumps(e.category_edges),
                         e.price_lead_score, json.dumps(e.price_lead_drivers),
                         e.post_entry_drift_48h, e.post_entry_drift_samples,
                         e.longshot_win_rate, e.longshot_implied_rate,
                         e.longshot_events,
                         e.late_entry_win_rate, e.late_entry_implied_rate,
                         e.late_entry_events,
                         e.style_archetype, e.automation_score,
                         json.dumps(e.style_drivers),
                         e.trades_per_active_day, e.median_intertrade_gap_seconds,
                         e.active_utc_hours, e.buy_size_uniformity,
                         e.markets_per_active_day, e.top_category,
                         e.top_category_share, e.exited_position_share,
                         e.median_exit_hold_hours,
                         e.data_quality_status, json.dumps(e.data_quality_reasons),
                         e.economic_qualified, e.tailability_status,
                         json.dumps(e.tailability_reasons), e.score_version, e.computed_at)
                        for e in enrichments
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(enrichments)


class PostgresWalletBetStore:
    """Upsert on (proxy_wallet, condition_id, outcome_index)."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_bets(self, bets: list[PolymarketWalletBet]) -> int:
        if not bets:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_wallet_bets
                        (proxy_wallet, condition_id, outcome_index, outcome,
                         event_slug, category, title, status, entry_price,
                         cost_usdc, net_size, realized_pnl_usdc, total_pnl_usdc,
                         clv, last_trade_ts, computed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet, condition_id, outcome_index)
                    DO UPDATE SET
                        outcome           = EXCLUDED.outcome,
                        event_slug        = EXCLUDED.event_slug,
                        category          = EXCLUDED.category,
                        title             = EXCLUDED.title,
                        status            = EXCLUDED.status,
                        entry_price       = EXCLUDED.entry_price,
                        cost_usdc         = EXCLUDED.cost_usdc,
                        net_size          = EXCLUDED.net_size,
                        realized_pnl_usdc = EXCLUDED.realized_pnl_usdc,
                        total_pnl_usdc    = EXCLUDED.total_pnl_usdc,
                        clv               = EXCLUDED.clv,
                        last_trade_ts     = EXCLUDED.last_trade_ts,
                        computed_at       = EXCLUDED.computed_at
                    """,
                    [
                        (b.proxy_wallet, b.condition_id, b.outcome_index, b.outcome,
                         b.event_slug, b.category, b.title, b.status, b.entry_price,
                         b.cost_usdc, b.net_size, b.realized_pnl_usdc, b.total_pnl_usdc,
                         b.clv, b.last_trade_ts, b.computed_at)
                        for b in bets
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(bets)


class PostgresPriceSnapshotStore:
    """Append-only; duplicate (condition_id, fetched_at) points are ignored."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def write_snapshots(self, snapshots: list[PolymarketPriceSnapshot]) -> int:
        if not snapshots:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO polymarket_price_snapshots
                        (condition_id, yes_price, active, closed, fetched_at)
                    VALUES (%s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (condition_id, fetched_at) DO NOTHING
                    """,
                    [
                        (s.condition_id, s.yes_price, s.active, s.closed, s.fetched_at)
                        for s in snapshots
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(snapshots)


class PostgresMarketLinkStore:
    """
    Mirrors JsonlMarketLinkStore.upsert_links semantics:
      - Manual decisions are sticky (auto runs can't overwrite them).
      - Auto runs CAN update other auto rows.

    The WHERE clause on the ON CONFLICT branch blocks the update when the
    existing row is manual. Returned `written` counts incoming links that
    were not blocked by a manual decision (matching the JSONL store's count).
    """

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def load_links(self) -> list[MarketLink]:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT kalshi_ticker, polymarket_condition_id, polymarket_slug,
                           kalshi_title, polymarket_title, kalshi_end_date,
                           polymarket_end_date, confidence, status, matched_by,
                           matched_at
                    FROM polymarket_market_links
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        out: list[MarketLink] = []
        for r in rows:
            matched_at_raw = r[10]
            matched_at = (
                matched_at_raw.isoformat().replace("+00:00", "Z")
                if hasattr(matched_at_raw, "isoformat")
                else str(matched_at_raw)
            )
            out.append(MarketLink(
                kalshi_ticker=r[0],
                polymarket_condition_id=r[1],
                polymarket_slug=r[2],
                kalshi_title=r[3],
                polymarket_title=r[4],
                kalshi_end_date=r[5],
                polymarket_end_date=r[6],
                confidence=float(r[7]),
                status=r[8],
                matched_by=r[9],
                matched_at=matched_at,
            ))
        return out

    def upsert_links(self, links: list[MarketLink]) -> int:
        if not links:
            return 0
        written = 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                for link in links:
                    cur.execute(
                        """
                        SELECT matched_by
                        FROM polymarket_market_links
                        WHERE kalshi_ticker = %s AND polymarket_condition_id = %s
                        """,
                        (link.kalshi_ticker, link.polymarket_condition_id),
                    )
                    row = cur.fetchone()
                    if row is not None and row[0] == "manual":
                        # Sticky-manual: skip and don't count.
                        continue
                    cur.execute(
                        """
                        INSERT INTO polymarket_market_links
                            (kalshi_ticker, polymarket_condition_id, polymarket_slug,
                             kalshi_title, polymarket_title, kalshi_end_date,
                             polymarket_end_date, confidence, status, matched_by,
                             matched_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                        ON CONFLICT (kalshi_ticker, polymarket_condition_id) DO UPDATE SET
                            polymarket_slug      = EXCLUDED.polymarket_slug,
                            kalshi_title         = EXCLUDED.kalshi_title,
                            polymarket_title     = EXCLUDED.polymarket_title,
                            kalshi_end_date      = EXCLUDED.kalshi_end_date,
                            polymarket_end_date  = EXCLUDED.polymarket_end_date,
                            confidence           = EXCLUDED.confidence,
                            status               = EXCLUDED.status,
                            matched_by           = EXCLUDED.matched_by,
                            matched_at           = EXCLUDED.matched_at
                        """,
                        (link.kalshi_ticker, link.polymarket_condition_id,
                         link.polymarket_slug, link.kalshi_title, link.polymarket_title,
                         link.kalshi_end_date, link.polymarket_end_date, link.confidence,
                         link.status, link.matched_by, link.matched_at),
                    )
                    written += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return written


class PostgresWalletCheckpointStore:
    """Monotonic per-wallet watermark. ON CONFLICT keeps the GREATEST value,
    so an out-of-order older fetch never rewinds the checkpoint."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url

    def get_last_timestamp(self, wallet: str) -> int | None:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_timestamp FROM polymarket_wallet_checkpoints "
                    "WHERE proxy_wallet = %s",
                    (wallet.lower(),),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return int(row[0]) if row is not None else None

    def set_last_timestamp(self, wallet: str, timestamp: int) -> None:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO polymarket_wallet_checkpoints (proxy_wallet, last_timestamp)
                    VALUES (%s, %s)
                    ON CONFLICT (proxy_wallet) DO UPDATE
                        SET last_timestamp = GREATEST(
                            polymarket_wallet_checkpoints.last_timestamp,
                            EXCLUDED.last_timestamp
                        )
                    """,
                    (wallet.lower(), timestamp),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── Dual-write wrappers (Phase 7.5) ──────────────────────────────────────────
#
# When DATABASE_URL is set, the runner wraps each JSONL store with the matching
# Dual* class so every write fans out to BOTH JSONL and Postgres. The API still
# reads from JSONL, so a Postgres outage degrades writes (caller sees the
# exception) without breaking the read path. The reported `written` count is
# the JSONL count — that remains the source of truth for the API.

class DualLeaderboardStore:
    def __init__(self, primary: LeaderboardStore, secondary: LeaderboardStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int:
        written = self._primary.write_leaderboard(entries)
        self._secondary.write_leaderboard(entries)
        return written


class DualActivityStore:
    def __init__(self, primary: ActivityStore, secondary: ActivityStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_activity(self, events: list[PolymarketActivity]) -> int:
        written = self._primary.write_activity(events)
        self._secondary.write_activity(events)
        return written

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()


class DualPositionStore:
    def __init__(self, primary: PositionStore, secondary: PositionStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_positions(self, positions: list[PolymarketPosition]) -> int:
        written = self._primary.write_positions(positions)
        self._secondary.write_positions(positions)
        return written


class DualPositionSnapshotStore:
    def __init__(
        self, primary: PositionSnapshotStore, secondary: PositionSnapshotStore
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_position_snapshot(self, snapshot: PolymarketPositionSnapshot) -> int:
        written = self._primary.write_position_snapshot(snapshot)
        self._secondary.write_position_snapshot(snapshot)
        return written


class DualClosedPositionStore:
    def __init__(
        self, primary: ClosedPositionStore, secondary: ClosedPositionStore
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_closed_positions(self, positions: list[PolymarketClosedPosition]) -> int:
        written = self._primary.write_closed_positions(positions)
        self._secondary.write_closed_positions(positions)
        return written


class DualWalletHydrationStore:
    def __init__(
        self, primary: WalletHydrationStore, secondary: WalletHydrationStore
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def load_hydration(self) -> dict[str, PolymarketWalletHydration]:
        return self._primary.load_hydration()

    def upsert_hydration(self, rows: list[PolymarketWalletHydration]) -> int:
        written = self._primary.upsert_hydration(rows)
        self._secondary.upsert_hydration(rows)
        return written


class DualMarketStore:
    def __init__(self, primary: MarketStore, secondary: MarketStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_markets(self, markets: list[PolymarketMarket]) -> int:
        written = self._primary.write_markets(markets)
        self._secondary.write_markets(markets)
        return written


class DualWalletValueStore:
    def __init__(self, primary: WalletValueStore, secondary: WalletValueStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_values(self, values: list[PolymarketWalletValue]) -> int:
        written = self._primary.write_values(values)
        self._secondary.write_values(values)
        return written


class DualWalletBetStore:
    def __init__(self, primary: WalletBetStore, secondary: WalletBetStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_bets(self, bets: list[PolymarketWalletBet]) -> int:
        written = self._primary.write_bets(bets)
        self._secondary.write_bets(bets)
        return written


class DualPriceSnapshotStore:
    def __init__(
        self, primary: PriceSnapshotStore, secondary: PriceSnapshotStore
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_snapshots(self, snapshots: list[PolymarketPriceSnapshot]) -> int:
        written = self._primary.write_snapshots(snapshots)
        self._secondary.write_snapshots(snapshots)
        return written


class DualEnrichmentStore:
    def __init__(self, primary: EnrichmentStore, secondary: EnrichmentStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_enrichment(self, enrichments: list[PolymarketWalletEnrichment]) -> int:
        written = self._primary.write_enrichment(enrichments)
        self._secondary.write_enrichment(enrichments)
        return written


class DualWalletCheckpointStore:
    """Writes fan out to both; reads come from the primary (JSONL) since that
    is the system-of-record while we're in dual-write mode."""

    def __init__(
        self, primary: WalletCheckpointStore, secondary: WalletCheckpointStore
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def get_last_timestamp(self, wallet: str) -> int | None:
        return self._primary.get_last_timestamp(wallet)

    def set_last_timestamp(self, wallet: str, timestamp: int) -> None:
        self._primary.set_last_timestamp(wallet, timestamp)
        self._secondary.set_last_timestamp(wallet, timestamp)


class DualMarketLinkStore:
    """Both stores enforce sticky-manual independently. Reads come from the
    primary (JSONL) so downstream matcher behavior is unchanged."""

    def __init__(self, primary: MarketLinkStore, secondary: MarketLinkStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def load_links(self) -> list[MarketLink]:
        return self._primary.load_links()

    def upsert_links(self, links: list[MarketLink]) -> int:
        written = self._primary.upsert_links(links)
        self._secondary.upsert_links(links)
        return written
