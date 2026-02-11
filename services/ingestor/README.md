# MarketSignalOS Ingestor

This package contains ingestion workers for prediction market data sources.

## Initial scope (Kalshi)
- REST pagination for market metadata and trade events
- Basic retry handling for HTTP 429/5xx responses
- Normalized event model used by downstream storage/signal systems

## Quick start
```bash
cd services/ingestor
python -m pip install -e '.[dev]'
pytest
```

## Environment variables
- `KALSHI_BASE_URL`: defaults to `https://api.elections.kalshi.com/trade-api/v2`
- `KALSHI_API_KEY`: optional for authenticated endpoints
