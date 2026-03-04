from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from .kalshi_client import KalshiClient, KalshiClientConfig
from .pipeline import KalshiFillIngestionPipeline, KalshiResolutionIngestionPipeline
from .storage import JsonCheckpointStore, JsonlFillStore, JsonlResolutionStore
from .worker import KalshiFillIngestionWorker, KalshiResolutionIngestionWorker


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
    fill_store = JsonlFillStore(data_dir / "kalshi_fills.jsonl")
    resolution_store = JsonlResolutionStore(data_dir / "kalshi_market_resolutions.jsonl")

    client = KalshiClient(KalshiClientConfig.from_env())
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
    interval_seconds = _interval_seconds()

    try:
        while not stop:
            for ticker in tickers:
                result = fill_worker.ingest_once(ticker, limit=500)
                print(
                    f"[fills] ticker={ticker} previous={result.previous_cursor} "
                    f"next={result.next_cursor} written={result.records_written}"
                )

            market_result = resolution_worker.ingest_once(limit=200)
            print(
                f"[resolutions] previous={market_result.previous_cursor} "
                f"next={market_result.next_cursor} written={market_result.records_written}"
            )

            if stop:
                break
            time.sleep(interval_seconds)
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
