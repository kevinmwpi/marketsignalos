# marketsignalos
Detect statistically abnormal behavior and information asymmetry.


Production-grade market surveillance + mispricing detection system:
- Prediction markets: abnormal behavior & information-asymmetry signals
- Options: deterministic mispricing (“pseudo-arbitrage”) scanners
- First-class observability: signal freshness, ingestion lag, completeness

## Repo layout
- `apps/api`: FastAPI API
- `apps/web`: Next.js dashboard
- `services/ingestor`: ingestion workers
- `services/signals`: reusable signal engine library
- `infra`: local + deployment infrastructure
- `docs`: PRD, architecture, runbook, decisions (ADRs)

## Local development (placeholder)
- `docker-compose up` (coming soon)

## Status
Early development (M0 bootstrap).
