# MarketSignalOS Ingestor

This package contains ingestion workers for prediction market data sources.

## What this stores (today)
Ingestion **does not feed the website directly**. The minimum viable path is:

1. Pull Kalshi trades via API.
2. Normalize payloads into `NormalizedTrade`.
3. Persist records to local JSONL (`data/kalshi_trades.jsonl`).
4. Persist per-ticker cursor checkpoints in JSON (`data/kalshi_checkpoints.json`).
5. Persist account-level fills (`data/kalshi_fills.jsonl`) and settled outcomes (`data/kalshi_market_resolutions.jsonl`) for suspicious-account scoring.

This gives replayable ingestion state and avoids calling Kalshi on every page refresh.

## Will every website refresh call Kalshi?
No. The intended architecture is:
- background ingestor pulls from Kalshi on a schedule,
- API reads from stored data,
- web frontend calls API.

So refreshes hit our API/storage layer, not Kalshi directly.

## Minimum required code implemented
- Kalshi HTTP client (`kalshi_client.py`)
- trade normalization pipeline (`pipeline.py`)
- local trade + checkpoint storage (`storage.py`)
- single-run worker orchestration (`worker.py`)
- continuous runner entrypoint (`runner.py`)

## Quick start
```bash
cd services/ingestor
python -m pip install -e '.[dev]'
pytest
```

## Environment variables
- `KALSHI_BASE_URL`: defaults to `https://api.elections.kalshi.com/trade-api/v2`
- `KALSHI_API_KEY`: optional bearer token fallback
- `KALSHI_API_KEY_ID`: Kalshi key id for keypair auth
- `KALSHI_PRIVATE_KEY_PEM`: Kalshi private key PEM for request signing
- `INGEST_MARKET_TICKERS`: comma-separated Kalshi tickers for trade and fill polling
- `INGEST_CONTINUOUS`: optional (`true`/`false`), defaults to `false` for one-shot batch runs
- `INGEST_INTERVAL_SECONDS`: polling interval in seconds when `INGEST_CONTINUOUS=true`

## Run one-shot batch ingestion (recommended)
```bash
cd services/ingestor
python -m marketsignalos_ingestor.runner
```

Use an external scheduler (cron/Task Scheduler/GitHub Actions) to run this command periodically.

## Run continuous ingestion (optional)
```bash
cd services/ingestor
INGEST_CONTINUOUS=true INGEST_INTERVAL_SECONDS=30 python -m marketsignalos_ingestor.runner
```
