# MarketSignalOS Ingestor

This package contains ingestion workers for prediction market data sources.

## What this stores (today)
Ingestion **does not feed the website directly**. The minimum viable path is:

1. Pull Kalshi trades via API.
2. Normalize payloads into `NormalizedTrade`.
3. Persist records to local JSONL (`data/kalshi_trades.jsonl`).
4. Persist per-ticker cursor checkpoints in JSON (`data/kalshi_checkpoints.json`).

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
