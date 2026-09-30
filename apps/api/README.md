# MarketSignalOS API

FastAPI serves public wallet research feeds and private operator controls.
See the repository [quick start](../../README.md) and
[Railway deployment guide](../../docs/railway-deployment.md).

## Access

Public GET routes include `/health`, `/platform/status`,
`/signals/skilled-bets`, `/signals/skilled-bets/summary`,
`/signals/polymarket-leaderboard`, `/signals/wallets/{wallet}`,
`/signals/ledger`, `/signals/market-consensus`, and `/signals/exits`.
The complete current schema is available at `/docs`.

All POST routes, `/ingestor/status`, `/signals/notifications/status`, and
`/metrics` require `Authorization: Bearer <ADMIN_API_TOKEN>`.
Without a configured token these controls return 503. Local development can
explicitly set `ALLOW_UNAUTHENTICATED_ADMIN=1`; production ignores this bypass.

## Storage and scheduling

The API reads JSONL from `POLYMARKET_DATA_DIR`. The optional Postgres ingestion
mirror is not its read store. Railway requires a persistent volume, one worker
and one replica. `INGEST_EVERY_MINUTES` enables the in-process scheduler, and
`INGEST_DEEP_EVERY_N_RUNS` sets the discovery cadence. Run receipts persist on disk.

`/health` reports process liveness. `/platform/status` reports processing
freshness and sampled coverage, without exposing private logs or error messages.

## Verification

From the repository root, with both Python packages installed:

```powershell
.\.venv\Scripts\python.exe -m pytest -q apps/api services/polymarket-ingestor
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe apps/api
```
