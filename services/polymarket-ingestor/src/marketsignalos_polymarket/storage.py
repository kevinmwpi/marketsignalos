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
    PolymarketActivity,
    PolymarketLeaderboardEntry,
    PolymarketMarket,
    PolymarketPosition,
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


# ── Postgres stubs (Phase 7) ──────────────────────────────────────────────────

class _PostgresStoreStub:
    def __init__(self, database_url: str) -> None:  # pragma: no cover
        self._database_url = database_url

    def _not_implemented(self) -> None:
        raise NotImplementedError(
            "Polymarket Postgres stores are not yet implemented — "
            "use the JSONL stores until Phase 7 migration lands."
        )


class PostgresLeaderboardStore(_PostgresStoreStub):
    def write_leaderboard(self, entries: list[PolymarketLeaderboardEntry]) -> int:
        del entries
        self._not_implemented()
        return 0


class PostgresActivityStore(_PostgresStoreStub):
    def write_activity(self, events: list[PolymarketActivity]) -> int:
        del events
        self._not_implemented()
        return 0


class PostgresPositionStore(_PostgresStoreStub):
    def write_positions(self, positions: list[PolymarketPosition]) -> int:
        del positions
        self._not_implemented()
        return 0


class PostgresMarketStore(_PostgresStoreStub):
    def write_markets(self, markets: list[PolymarketMarket]) -> int:
        del markets
        self._not_implemented()
        return 0
