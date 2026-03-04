from __future__ import annotations

from pathlib import Path

from marketsignalos_ingestor.pipeline import (
    KalshiFillIngestionPipeline,
    KalshiResolutionIngestionPipeline,
    KalshiTradeIngestionPipeline,
)
from marketsignalos_ingestor.storage import (
    JsonCheckpointStore,
    JsonlFillStore,
    JsonlResolutionStore,
    JsonlTradeStore,
)
from marketsignalos_ingestor.worker import (
    KalshiFillIngestionWorker,
    KalshiIngestionWorker,
    KalshiResolutionIngestionWorker,
)


class StubKalshiClient:
    def list_trades(self, ticker: str, *, limit: int = 500, cursor: str | None = None) -> dict[str, object]:
        assert ticker == "KXTEST"
        assert limit == 25
        assert cursor == "prev-cursor"
        return {
            "cursor": "next-cursor",
            "trades": [
                {
                    "trade_id": "trade-1",
                    "side": "no",
                    "yes_price": 40,
                    "no_price": 60,
                    "count": 3,
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
        }


def test_worker_ingest_once_reads_and_updates_checkpoint(tmp_path: Path) -> None:
    checkpoint_store = JsonCheckpointStore(tmp_path / "checkpoints.json")
    checkpoint_store.set_cursor("KXTEST", "prev-cursor")
    trade_store = JsonlTradeStore(tmp_path / "trades.jsonl")

    pipeline = KalshiTradeIngestionPipeline(StubKalshiClient())  # type: ignore[arg-type]
    worker = KalshiIngestionWorker(pipeline, trade_store, checkpoint_store)

    result = worker.ingest_once("KXTEST", limit=25)

    assert result.previous_cursor == "prev-cursor"
    assert result.next_cursor == "next-cursor"
    assert result.records_written == 1
    assert checkpoint_store.get_cursor("KXTEST") == "next-cursor"


class StubKalshiFillClient:
    def list_fills(self, ticker: str, *, limit: int = 500, cursor: str | None = None) -> dict[str, object]:
        assert ticker == "KXFILL"
        assert limit == 10
        assert cursor == "fill-prev"
        return {
            "cursor": "fill-next",
            "fills": [
                {
                    "member_id": "acct-1",
                    "fill_id": "fill-1",
                    "trade_id": "trade-1",
                    "side": "yes",
                    "yes_price": 60,
                    "no_price": 40,
                    "count": 2,
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
        }


def test_fill_worker_ingest_once_reads_and_updates_checkpoint(tmp_path: Path) -> None:
    checkpoint_store = JsonCheckpointStore(tmp_path / "checkpoints.json")
    checkpoint_store.set_cursor("fills:KXFILL", "fill-prev")
    fill_store = JsonlFillStore(tmp_path / "fills.jsonl")

    pipeline = KalshiFillIngestionPipeline(StubKalshiFillClient())  # type: ignore[arg-type]
    worker = KalshiFillIngestionWorker(pipeline, fill_store, checkpoint_store)

    result = worker.ingest_once("KXFILL", limit=10)

    assert result.previous_cursor == "fill-prev"
    assert result.next_cursor == "fill-next"
    assert result.records_written == 1
    assert checkpoint_store.get_cursor("fills:KXFILL") == "fill-next"


class StubKalshiMarketsClient:
    def list_markets(
        self,
        *,
        status: str = "settled",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, object]:
        assert status == "settled"
        assert limit == 10
        assert cursor == "market-prev"
        return {
            "cursor": "market-next",
            "markets": [
                {"ticker": "KX", "result": "no", "close_time": "2026-01-02T00:00:00Z"},
            ],
        }


def test_resolution_worker_ingest_once_reads_and_updates_checkpoint(tmp_path: Path) -> None:
    checkpoint_store = JsonCheckpointStore(tmp_path / "checkpoints.json")
    checkpoint_store.set_cursor("markets:settled", "market-prev")
    resolution_store = JsonlResolutionStore(tmp_path / "resolutions.jsonl")

    pipeline = KalshiResolutionIngestionPipeline(StubKalshiMarketsClient())  # type: ignore[arg-type]
    worker = KalshiResolutionIngestionWorker(pipeline, resolution_store, checkpoint_store)

    result = worker.ingest_once(limit=10)

    assert result.previous_cursor == "market-prev"
    assert result.next_cursor == "market-next"
    assert result.records_written == 1
    assert checkpoint_store.get_cursor("markets:settled") == "market-next"
