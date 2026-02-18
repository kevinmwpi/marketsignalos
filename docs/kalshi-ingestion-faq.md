# Kalshi ingestion FAQ (current implementation)

## Where are we ingesting data to?
For the minimum implementation in this repo, ingested trades are stored locally in:
- JSONL trade log (append-only)
- JSON checkpoint file keyed by ticker cursor

## Is data directly displayed on the website?
No. Ingestion should write to storage first. API/web should read from that storage layer.

## Will every refresh call Kalshi API?
No. Refreshes should hit our API, and API should serve cached/persisted data from our store.
Kalshi should be called only by a background ingestion worker.

## What is the minimum required code?
1. HTTP client to fetch Kalshi trades with retries.
2. Normalization layer from Kalshi payload -> canonical trade model.
3. Storage writer for normalized records.
4. Checkpoint store for pagination cursor.
5. Worker loop (or `ingest_once`) that ties these steps together.

## Which GitHub secrets are required for Kalshi keypair auth?
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PEM`

These should be injected into worker runtime env vars with the same names.
