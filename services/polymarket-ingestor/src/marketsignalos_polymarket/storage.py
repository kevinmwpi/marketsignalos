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
from typing import Protocol

from .models import (
    MarketLink,
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
    PolymarketWalletEnrichment,
    PolymarketWalletValue,
)


# ── Protocols ─────────────────────────────────────────────────────────────────

class LeaderboardStore(Protocol):
    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int: ...


class ActivityStore(Protocol):
    def write_activity(self, events: list[PolymarketActivity]) -> int: ...


class PositionStore(Protocol):
    def write_positions(self, positions: list[PolymarketPosition]) -> int: ...


class MarketStore(Protocol):
    def write_markets(self, markets: list[PolymarketMarket]) -> int: ...


class WalletValueStore(Protocol):
    def write_values(self, values: list[PolymarketWalletValue]) -> int: ...


class WalletCheckpointStore(Protocol):
    def get_last_timestamp(self, wallet: str) -> int | None: ...
    def set_last_timestamp(self, wallet: str, timestamp: int) -> None: ...


class EnrichmentStore(Protocol):
    def write_enrichment(self, enrichments: list[PolymarketWalletEnrichment]) -> int: ...


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
    index_path.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")


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


class JsonlActivityStore:
    """Append + dedupe on (transaction_hash, condition_id, outcome_index)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path = path.with_suffix(path.suffix + ".index.json")

    def write_activity(self, events: list[PolymarketActivity]) -> int:
        if not events:
            return 0
        index = _read_index(self._index_path)
        written = 0
        with self._path.open("a", encoding="utf-8") as fh:
            for event in events:
                key = self._dedupe_key(event)
                if key in index:
                    continue
                index.add(key)
                fh.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")
                written += 1
        _write_index(self._index_path, index)
        return written

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


class JsonlMarketStore:
    """Append + dedupe on condition_id. Closed-market re-fetches overwrite by adding
    a new row; downstream readers should keep the most recent fetched_at."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path = path.with_suffix(path.suffix + ".index.json")

    def write_markets(self, markets: list[PolymarketMarket]) -> int:
        if not markets:
            return 0
        # We DO want to refresh closed/active status, so dedupe is by
        # (condition_id, closed_bool) — lets us record the transition.
        index = _read_index(self._index_path)
        written = 0
        with self._path.open("a", encoding="utf-8") as fh:
            for market in markets:
                key = f"{market.condition_id}:{market.closed}"
                if key in index:
                    continue
                index.add(key)
                fh.write(json.dumps(asdict(market), separators=(",", ":")) + "\n")
                written += 1
        _write_index(self._index_path, index)
        return written


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
                         size, avg_price, current_value_usdc,
                         slug, title, event_slug, snapshot_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (proxy_wallet, condition_id, outcome_index, snapshot_at)
                        DO NOTHING
                    """,
                    [
                        (p.proxy_wallet, p.condition_id, p.outcome_index, p.outcome,
                         p.size, p.avg_price, p.current_value_usdc,
                         p.slug, p.title, p.event_slug, p.snapshot_at)
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
                         trade_count, last_activity_at, computed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s::timestamptz)
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
                         e.trade_count, e.last_activity_at, e.computed_at)
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


class DualPositionStore:
    def __init__(self, primary: PositionStore, secondary: PositionStore) -> None:
        self._primary = primary
        self._secondary = secondary

    def write_positions(self, positions: list[PolymarketPosition]) -> int:
        written = self._primary.write_positions(positions)
        self._secondary.write_positions(positions)
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
