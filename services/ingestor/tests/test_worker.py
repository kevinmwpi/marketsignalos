from __future__ import annotations

from pathlib import Path

from marketsignalos_ingestor.pipeline import KalshiTradeIngestionPipeline
from marketsignalos_ingestor.storage import JsonCheckpointStore, JsonlTradeStore
from marketsignalos_ingestor.worker import KalshiIngestionWorker


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
