from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .models import MarketResolution, NormalizedFill, NormalizedTrade


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
