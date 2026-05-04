from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from .kalshi_client import KalshiClient, KalshiClientConfig
from .pipeline import (
    KalshiFillIngestionPipeline,
    KalshiResolutionIngestionPipeline,
    KalshiTradeIngestionPipeline,
)
from .storage import JsonCheckpointStore, JsonlFillStore, JsonlResolutionStore, JsonlTradeStore
from .worker import KalshiFillIngestionWorker, KalshiIngestionWorker, KalshiResolutionIngestionWorker


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _data_dir() -> Path:
    configured = os.getenv("INGESTOR_DATA_DIR")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data"


def _tickers() -> list[str]:
    raw = os.getenv("INGEST_MARKET_TICKERS", "")
    out = [ticker.strip() for ticker in raw.split(",") if ticker.strip()]
    if not out:
        raise ValueError("INGEST_MARKET_TICKERS must contain at least one ticker")
    return out


def _interval_seconds() -> float:
    raw = os.getenv("INGEST_INTERVAL_SECONDS", "30")
    value = float(raw)
    if value <= 0:
        raise ValueError("INGEST_INTERVAL_SECONDS must be > 0")
    return value


def _continuous_mode() -> bool:
    raw = os.getenv("INGEST_CONTINUOUS", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    stop = False

    def _stop_handler(signum: int, frame: object) -> None:
        del signum, frame
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    checkpoints = JsonCheckpointStore(data_dir / "kalshi_checkpoints.json")
    trade_store = JsonlTradeStore(data_dir / "kalshi_trades.jsonl")
    fill_store = JsonlFillStore(data_dir / "kalshi_fills.jsonl")
    resolution_store = JsonlResolutionStore(data_dir / "kalshi_market_resolutions.jsonl")

    client = KalshiClient(KalshiClientConfig.from_env())
    trade_worker = KalshiIngestionWorker(
        KalshiTradeIngestionPipeline(client),
        trade_store,
        checkpoints,
    )
    fill_worker = KalshiFillIngestionWorker(
        KalshiFillIngestionPipeline(client),
        fill_store,
        checkpoints,
    )
    resolution_worker = KalshiResolutionIngestionWorker(
        KalshiResolutionIngestionPipeline(client),
        resolution_store,
        checkpoints,
    )

    tickers = _tickers()
    continuous = _continuous_mode()
    interval_seconds = _interval_seconds() if continuous else 0.0

    try:
        while not stop:
            total_trade_records = 0
            total_fill_records = 0
            for ticker in tickers:
                trade_result = trade_worker.ingest_once(ticker, limit=500)
                total_trade_records += trade_result.records_written
                print(
                    f"[trades] ticker={ticker} previous={trade_result.previous_cursor} "
                    f"next={trade_result.next_cursor} written={trade_result.records_written}"
                )

                result = fill_worker.ingest_once(ticker, limit=500)
                total_fill_records += result.records_written
                print(
                    f"[fills] ticker={ticker} previous={result.previous_cursor} "
                    f"next={result.next_cursor} written={result.records_written}"
                )

            market_result = resolution_worker.ingest_once(limit=200)
            print(
                f"[resolutions] previous={market_result.previous_cursor} "
                f"next={market_result.next_cursor} written={market_result.records_written}"
            )
            print(
                f"[summary] tickers={len(tickers)} trades_written={total_trade_records} "
                f"fills_written={total_fill_records} "
                f"resolutions_written={market_result.records_written}"
            )

            if stop or not continuous:
                break
            time.sleep(interval_seconds)
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
