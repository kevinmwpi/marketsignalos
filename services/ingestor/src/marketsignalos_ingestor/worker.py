from __future__ import annotations

from dataclasses import dataclass

from .pipeline import KalshiTradeIngestionPipeline
from .storage import CheckpointStore, TradeStore


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
