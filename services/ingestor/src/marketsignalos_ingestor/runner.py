from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

from .enrichment import compute_enrichment
from .kalshi_client import KalshiClient, KalshiClientConfig
from .pipeline import (
    KalshiFillIngestionPipeline,
    KalshiResolutionIngestionPipeline,
    KalshiTradeIngestionPipeline,
)
from .storage import (
    CheckpointStore,
    EnrichmentStore,
    FillStore,
    JsonCheckpointStore,
    JsonlEnrichmentStore,
    JsonlFillStore,
    JsonlResolutionStore,
    JsonlTradeStore,
    PostgresCheckpointStore,
    PostgresEnrichmentStore,
    PostgresFillStore,
    PostgresResolutionStore,
    PostgresTradeStore,
    ResolutionStore,
    TradeStore,
)
from .worker import KalshiFillIngestionWorker, KalshiIngestionWorker, KalshiResolutionIngestionWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("marketsignalos.ingestor")


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


def _validate_config() -> None:
    """Fail fast at startup if required configuration is missing."""
    _tickers()  # raises ValueError when empty

    has_bearer = bool(os.getenv("KALSHI_API_KEY"))
    has_keypair = bool(os.getenv("KALSHI_API_KEY_ID")) and bool(
        os.getenv("KALSHI_PRIVATE_KEY_PEM")
    )
    if not has_bearer and not has_keypair:
        raise ValueError(
            "Kalshi authentication is not configured. "
            "Set KALSHI_API_KEY for bearer auth, or both "
            "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM for keypair auth."
        )


def _build_stores(
    database_url: str | None,
    data_dir: Path,
) -> tuple[TradeStore, FillStore, ResolutionStore, CheckpointStore, EnrichmentStore]:
    if database_url:
        log.info("storage=postgres")
        return (
            PostgresTradeStore(database_url),
            PostgresFillStore(database_url),
            PostgresResolutionStore(database_url),
            PostgresCheckpointStore(database_url),
            PostgresEnrichmentStore(database_url),
        )
    log.info("storage=jsonl data_dir=%s", data_dir)
    return (
        JsonlTradeStore(data_dir / "kalshi_trades.jsonl"),
        JsonlFillStore(data_dir / "kalshi_fills.jsonl"),
        JsonlResolutionStore(data_dir / "kalshi_market_resolutions.jsonl"),
        JsonCheckpointStore(data_dir / "kalshi_checkpoints.json"),
        JsonlEnrichmentStore(data_dir / "kalshi_account_enrichment.jsonl"),
    )


def main() -> int:
    try:
        _validate_config()
    except ValueError as exc:
        log.error("startup validation failed: %s", exc)
        return 1

    stop = False

    def _stop_handler(signum: int, frame: object) -> None:
        del signum, frame
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    database_url = os.getenv("DATABASE_URL")
    trade_store, fill_store, resolution_store, checkpoints, enrichment_store = (
        _build_stores(database_url, data_dir)
    )

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

    log.info(
        "ingestor starting tickers=%s continuous=%s interval_seconds=%s",
        tickers,
        continuous,
        interval_seconds,
    )

    try:
        while not stop:
            total_trade_records = 0
            total_fill_records = 0

            for ticker in tickers:
                trade_result = trade_worker.ingest_once(ticker, limit=500)
                total_trade_records += trade_result.records_written
                log.info(
                    "batch component=trades ticker=%s previous=%s next=%s written=%d",
                    ticker,
                    trade_result.previous_cursor,
                    trade_result.next_cursor,
                    trade_result.records_written,
                )

                fill_result = fill_worker.ingest_once(ticker, limit=500)
                total_fill_records += fill_result.records_written
                log.info(
                    "batch component=fills ticker=%s previous=%s next=%s written=%d",
                    ticker,
                    fill_result.previous_cursor,
                    fill_result.next_cursor,
                    fill_result.records_written,
                )

            market_result = resolution_worker.ingest_once(limit=200)
            log.info(
                "batch component=resolutions previous=%s next=%s written=%d",
                market_result.previous_cursor,
                market_result.next_cursor,
                market_result.records_written,
            )

            # Recompute account enrichment after every ingestion cycle so that
            # insider_like_score in the API reflects fresh fill data.
            try:
                enrichments = compute_enrichment(
                    data_dir / "kalshi_fills.jsonl",
                    data_dir / "kalshi_market_resolutions.jsonl",
                )
                written_enrichments = enrichment_store.write_enrichment(enrichments)
                log.info("enrichment accounts_computed=%d", written_enrichments)
            except Exception as exc:
                log.warning("enrichment computation failed (non-fatal): %s", exc)

            log.info(
                "cycle_summary tickers=%d trades_written=%d fills_written=%d "
                "resolutions_written=%d",
                len(tickers),
                total_trade_records,
                total_fill_records,
                market_result.records_written,
            )

            if stop or not continuous:
                break
            time.sleep(interval_seconds)
    finally:
        client.close()
        log.info("ingestor stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
