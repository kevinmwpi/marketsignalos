# Architecture

## High-level
[External Data Sources]
  -> [Ingestor Workers]
  -> [Postgres/Timescale]
  -> [Signal Engine]
  -> [API (FastAPI)]
  -> [Web (Next.js)]

Everything emits:
- metrics (Prometheus)
- traces (OpenTelemetry)
- structured logs

## Key principles
- Deterministic signals first (explainable, testable)
- “Freshness” is a product feature
- No accusations: scores + statistical abnormality only
