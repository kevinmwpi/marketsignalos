# Kalshi ingestion: concrete next steps

This document turns the PRD-level goal ("ingest prediction market data") into an implementation plan we can execute in short milestones.

## 1) Define the canonical event contract

Before wiring storage or signals, lock down one normalized contract for trade data.

**Minimum fields:**
- `source` (`kalshi`)
- `market_ticker`
- `trade_id`
- `side` (`yes`/`no`)
- `price` (0-100 cents)
- `quantity`
- `traded_at` (UTC timestamp)

**Why first:** signal and storage logic should not care about Kalshi-specific payload shape.

## 2) Start with pull-based ingestion

Build/ship a reliable pull worker before introducing streaming:

1. Poll `GET /portfolio/trades` by ticker with cursor pagination.
2. Normalize each trade to our canonical model.
3. Persist a per-market checkpoint cursor.
4. Emit ingest metrics (`requests`, `retries`, `events_ingested`, `ingestion_lag_seconds`).

Pull mode gives deterministic replay behavior and easier recovery while we stabilize schemas.

## 3) Add resilience + correctness guardrails

Implement operational safety in the worker:

- Retry on `429` and `5xx` with exponential backoff.
- Respect `Retry-After` when present.
- Ensure idempotent writes keyed by `(source, trade_id)`.
- Alert on staleness if a market has no successful ingest window beyond SLA.

## 4) Storage shape for v1

For Postgres/Timescale, prefer append-only raw + deduped normalized tables:

- `raw_kalshi_trade_events`
- `prediction_market_trades`
- `ingestion_checkpoints`

This lets us replay transformations after schema changes without re-pulling from the provider.

## 5) Expand coverage after trade pipeline is stable

After trade ingestion is healthy:

- Add market metadata sync (`GET /markets`) for lifecycle and resolution timestamps.
- Add orderbook snapshot polling for slippage/liquidity metrics.
- Add backfill jobs for historical windows and completeness scoring.

## Suggested implementation order in this repo

1. Use `services/ingestor` package as the Kalshi adapter + normalization layer.
2. Add a worker entrypoint that runs `KalshiTradeIngestionPipeline` on a configured market list.
3. Add DB writer + checkpoint store abstraction.
4. Expose ingestion health in `apps/api` (`/health` plus source-specific freshness fields).

## Acceptance criteria for "M1 Kalshi ingest"

- Trades are ingested continuously for configured markets.
- Re-running the same window does not duplicate normalized records.
- Cursor checkpoints survive process restarts.
- Lag/freshness metrics are visible and alertable.
