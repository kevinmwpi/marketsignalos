from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .enrichment import ComputedEnrichment
from .models import MarketResolution, NormalizedFill, NormalizedTrade


# ── Protocols ─────────────────────────────────────────────────────────────────

class TradeStore(Protocol):
    def write_trades(self, trades: list[NormalizedTrade]) -> int:
        """Persist trades and return number of records written."""


class FillStore(Protocol):
    def write_fills(self, fills: list[NormalizedFill]) -> int:
        """Persist fills and return number of records written."""


class ResolutionStore(Protocol):
    def write_resolutions(self, resolutions: list[MarketResolution]) -> int:
        """Persist resolutions and return number of records written."""


class CheckpointStore(Protocol):
    def get_cursor(self, ticker: str) -> str | None:
        """Return last known cursor for ticker, if any."""

    def set_cursor(self, ticker: str, cursor: str | None) -> None:
        """Persist latest cursor for ticker."""


class EnrichmentStore(Protocol):
    def write_enrichment(self, enrichments: list[ComputedEnrichment]) -> int:
        """Overwrite the enrichment store and return records written."""


# ── JSONL implementations ──────────────────────────────────────────────────────

class JsonlTradeStore:
    """Simple append-only local store for normalized trades."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_trades(self, trades: list[NormalizedTrade]) -> int:
        if not trades:
            return 0
        with self._path.open("a", encoding="utf-8") as handle:
            for trade in trades:
                handle.write(json.dumps(asdict(trade), separators=(",", ":")) + "\n")
        return len(trades)


class JsonlFillStore:
    """Append-only local store with dedupe for account-level fills."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path = path.with_suffix(path.suffix + ".index.json")

    def write_fills(self, fills: list[NormalizedFill]) -> int:
        if not fills:
            return 0
        dedupe_index = self._read_index()
        written = 0
        with self._path.open("a", encoding="utf-8") as handle:
            for fill in fills:
                key = self._dedupe_key(fill)
                if key in dedupe_index:
                    continue
                dedupe_index.add(key)
                handle.write(json.dumps(asdict(fill), separators=(",", ":")) + "\n")
                written += 1
        self._write_index(dedupe_index)
        return written

    def _dedupe_key(self, fill: NormalizedFill) -> str:
        if fill.fill_id:
            return f"{fill.source}:{fill.fill_id}"
        return f"{fill.source}:{fill.trade_id}:{fill.account_id}:{fill.traded_at}"

    def _read_index(self) -> set[str]:
        if not self._index_path.exists():
            return set()
        raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("Fill index file must contain a JSON array")
        out: set[str] = set()
        for value in raw:
            if isinstance(value, str):
                out.add(value)
        return out

    def _write_index(self, values: set[str]) -> None:
        self._index_path.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")


class JsonlResolutionStore:
    """Append-only local store with dedupe for settled market outcomes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path = path.with_suffix(path.suffix + ".index.json")

    def write_resolutions(self, resolutions: list[MarketResolution]) -> int:
        if not resolutions:
            return 0
        dedupe_index = self._read_index()
        written = 0
        with self._path.open("a", encoding="utf-8") as handle:
            for resolution in resolutions:
                key = self._dedupe_key(resolution)
                if key in dedupe_index:
                    continue
                dedupe_index.add(key)
                handle.write(json.dumps(asdict(resolution), separators=(",", ":")) + "\n")
                written += 1
        self._write_index(dedupe_index)
        return written

    def _dedupe_key(self, resolution: MarketResolution) -> str:
        return f"{resolution.source}:{resolution.market_ticker}:{resolution.resolved_at}"

    def _read_index(self) -> set[str]:
        if not self._index_path.exists():
            return set()
        raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("Resolution index file must contain a JSON array")
        out: set[str] = set()
        for value in raw:
            if isinstance(value, str):
                out.add(value)
        return out

    def _write_index(self, values: set[str]) -> None:
        self._index_path.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")


class JsonCheckpointStore:
    """JSON file-based checkpoint persistence keyed by market ticker."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get_cursor(self, ticker: str) -> str | None:
        state = self._read_state()
        cursor = state.get(ticker)
        if cursor is None:
            return None
        if not isinstance(cursor, str):
            raise ValueError("Checkpoint cursor must be a string or null")
        return cursor

    def set_cursor(self, ticker: str, cursor: str | None) -> None:
        state = self._read_state()
        state[ticker] = cursor
        self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _read_state(self) -> dict[str, str | None]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Checkpoint file must contain a JSON object")
        out: dict[str, str | None] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError("Checkpoint keys must be ticker strings")
            if value is not None and not isinstance(value, str):
                raise ValueError("Checkpoint cursor must be a string or null")
            out[key] = value
        return out


class JsonlEnrichmentStore:
    """Overwrites enrichment JSONL on each write (recomputed from scratch each run)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write_enrichment(self, enrichments: list[ComputedEnrichment]) -> int:
        if not enrichments:
            return 0
        with self._path.open("w", encoding="utf-8") as handle:
            for enrichment in enrichments:
                handle.write(
                    json.dumps(asdict(enrichment), separators=(",", ":")) + "\n"
                )
        return len(enrichments)


# ── Postgres implementations ───────────────────────────────────────────────────

def _pg_connect(dsn: str):  # type: ignore[no-untyped-def]
    """Return a psycopg2 connection.  Import is deferred so tests that don't
    need Postgres can run without the psycopg2 package installed."""
    import psycopg2  # type: ignore[import-untyped]
    return psycopg2.connect(dsn)


class PostgresTradeStore:
    """Writes trades to Postgres; silently ignores duplicates via ON CONFLICT."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def write_trades(self, trades: list[NormalizedTrade]) -> int:
        if not trades:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO trades
                        (source, market_ticker, trade_id, side, price, quantity, traded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (source, trade_id) DO NOTHING
                    """,
                    [
                        (t.source, t.market_ticker, t.trade_id, t.side,
                         t.price, t.quantity, t.traded_at)
                        for t in trades
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(trades)


class PostgresFillStore:
    """Writes fills to Postgres; silently ignores duplicates via ON CONFLICT."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def write_fills(self, fills: list[NormalizedFill]) -> int:
        if not fills:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO fills
                        (source, account_id, market_ticker, fill_id, trade_id,
                         side, price, quantity, traded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (source, fill_id) DO NOTHING
                    """,
                    [
                        (f.source, f.account_id, f.market_ticker, f.fill_id,
                         f.trade_id, f.side, f.price, f.quantity, f.traded_at)
                        for f in fills
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(fills)


class PostgresResolutionStore:
    """Upserts resolved market outcomes (re-settling a market updates the record)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def write_resolutions(self, resolutions: list[MarketResolution]) -> int:
        if not resolutions:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO market_resolutions
                        (source, market_ticker, resolved_at, settlement_side)
                    VALUES (%s, %s, %s::timestamptz, %s)
                    ON CONFLICT (source, market_ticker) DO UPDATE
                        SET resolved_at     = EXCLUDED.resolved_at,
                            settlement_side = EXCLUDED.settlement_side
                    """,
                    [
                        (r.source, r.market_ticker, r.resolved_at, r.settlement_side)
                        for r in resolutions
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(resolutions)


class PostgresCheckpointStore:
    """Persists ingestor cursors in the ingestor_checkpoints table."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def get_cursor(self, key: str) -> str | None:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cursor FROM ingestor_checkpoints WHERE key = %s", (key,)
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        value = row[0]
        return str(value) if value is not None else None

    def set_cursor(self, key: str, cursor: str | None) -> None:
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingestor_checkpoints (key, cursor)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET cursor = EXCLUDED.cursor
                    """,
                    (key, cursor),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class PostgresEnrichmentStore:
    """Upserts per-account enrichment scores into account_enrichment."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def write_enrichment(self, enrichments: list[ComputedEnrichment]) -> int:
        if not enrichments:
            return 0
        conn = _pg_connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO account_enrichment
                        (account_id, cross_market_coordination,
                         pre_resolution_accuracy, fill_intensity, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (account_id) DO UPDATE
                        SET cross_market_coordination = EXCLUDED.cross_market_coordination,
                            pre_resolution_accuracy   = EXCLUDED.pre_resolution_accuracy,
                            fill_intensity            = EXCLUDED.fill_intensity,
                            updated_at                = now()
                    """,
                    [
                        (e.account_id, e.cross_market_coordination,
                         e.pre_resolution_accuracy, e.fill_intensity)
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
