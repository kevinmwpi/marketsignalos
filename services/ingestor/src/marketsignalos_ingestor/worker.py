from __future__ import annotations

from dataclasses import dataclass

from .pipeline import (
    KalshiFillIngestionPipeline,
    KalshiResolutionIngestionPipeline,
    KalshiTradeIngestionPipeline,
)
from .storage import CheckpointStore, FillStore, ResolutionStore, TradeStore


@dataclass(frozen=True, slots=True)
class IngestionRunResult:
    ticker: str
    previous_cursor: str | None
    next_cursor: str | None
    records_written: int


class KalshiIngestionWorker:
    """Minimum viable worker: read cursor -> pull batch -> persist trades + cursor."""

    def __init__(
        self,
        pipeline: KalshiTradeIngestionPipeline,
        trade_store: TradeStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self._pipeline = pipeline
        self._trade_store = trade_store
        self._checkpoint_store = checkpoint_store

    def ingest_once(self, ticker: str, *, limit: int = 500) -> IngestionRunResult:
        previous_cursor = self._checkpoint_store.get_cursor(ticker)
        batch = self._pipeline.pull_trade_batch(ticker=ticker, cursor=previous_cursor, limit=limit)

        records_written = self._trade_store.write_trades(batch.trades)
        self._checkpoint_store.set_cursor(ticker, batch.next_cursor)

        return IngestionRunResult(
            ticker=ticker,
            previous_cursor=previous_cursor,
            next_cursor=batch.next_cursor,
            records_written=records_written,
        )


class KalshiFillIngestionWorker:
    """MVP fill worker: read cursor -> pull fills -> persist fills + cursor."""

    def __init__(
        self,
        pipeline: KalshiFillIngestionPipeline,
        fill_store: FillStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self._pipeline = pipeline
        self._fill_store = fill_store
        self._checkpoint_store = checkpoint_store

    def ingest_once(self, ticker: str, *, limit: int = 500) -> IngestionRunResult:
        checkpoint_key = f"fills:{ticker}"
        previous_cursor = self._checkpoint_store.get_cursor(checkpoint_key)
        batch = self._pipeline.pull_fill_batch(ticker=ticker, cursor=previous_cursor, limit=limit)

        records_written = self._fill_store.write_fills(batch.fills)
        self._checkpoint_store.set_cursor(checkpoint_key, batch.next_cursor)

        return IngestionRunResult(
            ticker=ticker,
            previous_cursor=previous_cursor,
            next_cursor=batch.next_cursor,
            records_written=records_written,
        )


class KalshiResolutionIngestionWorker:
    """MVP resolution worker: read cursor -> pull settled markets -> persist + cursor."""

    def __init__(
        self,
        pipeline: KalshiResolutionIngestionPipeline,
        resolution_store: ResolutionStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self._pipeline = pipeline
        self._resolution_store = resolution_store
        self._checkpoint_store = checkpoint_store

    def ingest_once(self, *, limit: int = 200) -> IngestionRunResult:
        checkpoint_key = "markets:settled"
        previous_cursor = self._checkpoint_store.get_cursor(checkpoint_key)
        batch = self._pipeline.pull_resolution_batch(cursor=previous_cursor, limit=limit)

        records_written = self._resolution_store.write_resolutions(batch.resolutions)
        self._checkpoint_store.set_cursor(checkpoint_key, batch.next_cursor)

        return IngestionRunResult(
            ticker=checkpoint_key,
            previous_cursor=previous_cursor,
            next_cursor=batch.next_cursor,
            records_written=records_written,
        )
