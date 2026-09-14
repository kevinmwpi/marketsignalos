# PRD — MarketSignalOS (v1)

## Problem
Market data and market “signals” are noisy and failure-prone. Most tools surface signals without explicitly tracking data freshness, confidence, and failure modes.

## Goal
Build a production-grade system that:
- Ingests live + historical market data
- Computes explainable surveillance + mispricing signals
- Exposes observability as a first-class product feature

## Non-goals
- Automated trading
- Claiming “insider detection” or illegality
- ML-first prediction (v1 is deterministic)

## MVP Features
### Prediction Markets (Polymarket/Kalshi)
- Ingest trade stream / snapshots (market, price, size, time)
- Abnormal behavior signals:
  - pre-resolution accuracy spike
  - low-slippage high-accuracy footprint
  - cross-market correlated positioning
- Output: “Information Advantage Score (IAS)” + drivers (non-accusatory)

### Options (Mispricing)
- Ingest options chain snapshots
- Scanners:
  - intrinsic floor violations
  - put-call parity deviation
  - abnormal time-value decay
- Output: candidates + liquidity filters + persistence

### Observability
- Ingestion lag per source/market/symbol
- Completeness / backfill coverage
- Signal freshness SLA
- Error budgets

### API + Web
- `GET /signals`
- `GET /health`
- dashboard for signals + system health

## Success metrics
- <5s ingestion lag during active hours (where data allows)
- 100% backfill completeness for configured markets
- No silent failures (every drop is measurable)

## Risks
- rate limits, schema drift, missing participant identifiers, illiquidity
