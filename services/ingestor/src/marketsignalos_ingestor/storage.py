from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .models import NormalizedTrade


class TradeStore(Protocol):
    def write_trades(self, trades: list[NormalizedTrade]) -> int:
        """Persist trades and return number of records written."""


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
