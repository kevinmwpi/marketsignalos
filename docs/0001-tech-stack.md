# ADR 0001 — Tech stack selection

## Decision
Use:
- FastAPI (Python) for API
- Postgres + TimescaleDB for storage
- Redis + Celery for async jobs
- Prometheus + Grafana + OpenTelemetry for observability
- Next.js for web

## Context
We need time-series + relational joins, deterministic computations, and production-grade telemetry with minimal operational burden.

## Alternatives considered
- ClickHouse/InfluxDB (time-series only)
- Kafka (unnecessary for MVP complexity)
- Go backend (more setup; slower iteration)

## Consequences
- Clear typing + tests required for correctness
- Use hypertables for time-series tables
