# CLAUDE.md — MarketSignalOS

## What this project is

MarketSignalOS is a prediction market signal ranking system. It ingests live Kalshi trade data, computes statistical "information advantage" signals over participant accounts, and surfaces those signals through a FastAPI backend and a Next.js dashboard.

**Core value:** Rank prediction market accounts by *modeled skill* — win rate adjusted for statistical significance, anomaly scores, and behavioral patterns — not raw P&L.

**Non-goals (never build these):**
- Automated trading or order placement
- Claims of insider detection or illegal activity
- ML-first models in v1 (deterministic/explainable only)
- Kafka or event-streaming infrastructure

---

## Repository layout

```
marketsignalOS/
├── apps/
│   ├── api/           FastAPI backend — signal computation + REST endpoints
│   └── web/           Next.js 16 frontend dashboard
├── services/
│   └── ingestor/      Kalshi data ingestion workers
├── docs/              Architecture, PRD, ADRs, runbooks
├── scripts/           Deployment entry points (start-api.sh)
├── .github/workflows/ CI — Python (lint/type/test) + Node (lint/build)
├── pyproject.toml     Root Python tooling config (ruff, mypy, pytest paths)
├── railway.toml       Railway deployment config
└── Procfile           Single process: web → scripts/start-api.sh
```

---

## Dev setup

**Prerequisites:** Python 3.12, Node 20 (`.nvmrc`), a Postgres + TimescaleDB instance.

```powershell
# Python — create a shared venv at repo root
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
.\.venv\Scripts\pip install -e "services/ingestor[dev]"

# Node (web dashboard)
cd apps/web
npm ci
```

**Environment variables:**

| Variable | Where used | Notes |
|---|---|---|
| `PORT` | API server | Defaults to `8080`; Railway injects this automatically |
| `NEXT_PUBLIC_API_BASE_URL` | Next.js frontend | e.g. `http://localhost:8000` |
| `DATABASE_URL` | API + Ingestor | Postgres connection string (not yet wired in code) |
| `KALSHI_API_KEY` | Ingestor | Kalshi auth (not yet wired in code) |

---

## Running things locally

```powershell
# API (from repo root)
$env:PYTHONPATH = "apps/api/src"
.\.venv\Scripts\uvicorn marketsignalos_api.main:app --reload --port 8000

# Web dev server
cd apps/web
npm run dev   # http://localhost:3000

# Ingestor CLI
.\.venv\Scripts\marketsignalos-ingestor
```

---

## Testing

```powershell
# API tests
cd apps/api
..\..\  .venv\Scripts\python.exe -m pytest -q

# Ingestor tests
cd services/ingestor
..\..\  .venv\Scripts\python.exe -m pytest -q

# Both from repo root (after installing both packages)
.\.venv\Scripts\python.exe -m pytest -q apps/api services/ingestor

# Web
cd apps/web
npm run lint
npm run build
```

CI runs Python 3.12 on ubuntu-latest (ruff → mypy → pytest) and Node 20 (lint → build). Both must pass before merging.

---

## Linting and type checking

```powershell
# Python — from repo root
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy .

# TypeScript
cd apps/web && npm run lint
```

- Ruff: 100-char lines, Python 3.12 target
- MyPy: strict mode — all public functions need type annotations
- ESLint 9 flat config for TypeScript/Next.js

Fix lint before committing; CI enforces both.

---

## Architecture and data flow

```
Kalshi API
  → Ingestor workers (services/ingestor)
  → Postgres + TimescaleDB  (hypertables for time-series)
  → Signal Engine (apps/api/services/)
  → FastAPI endpoints (apps/api/api/routes/)
  → Next.js dashboard (apps/web)
```

Every layer emits:
- **Prometheus metrics** — `/metrics` endpoint on the API
- **Structured logs** — JSON, no print statements
- **OpenTelemetry traces** — infrastructure wired, not fully propagated yet

### Key API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML landing page with live leaderboard |
| `GET` | `/health` | Health check (used by Railway) |
| `GET` | `/metrics` | Prometheus text exposition |
| `GET` | `/signals/leaderboard` | Ranked accounts by modeled skill |
| `GET` | `/signals/trades` | Recent trade stream |
| `GET` | `/signals/orderflow` | Orderflow anomaly signals |
| `GET` | `/signals/opportunities` | Ranked trade opportunities |
| `GET` | `/docs` | Auto-generated Swagger UI |

---

## Signal design principles

- **Deterministic first.** Every score must be reproducible from the same input data. No model weights, no stochastic components in v1.
- **Explainable drivers.** Every top-level score (IAS, skill likelihood, anomaly probability) must decompose into human-readable sub-signals.
- **Non-accusatory language.** Use "information advantage", "anomaly probability", "insider-like score" — never "insider trading" or "manipulation".
- **Freshness is a product feature.** Every signal surface includes `last_activity_at` and staleness guards (`fresh_days` param).

### Core scoring concepts

| Signal | Description |
|---|---|
| `skill_likelihood` | Binomial model: how unlikely is this win rate by chance given N resolved markets |
| `insider_like_score` | Composite of pre-resolution accuracy, low-slippage footprint, cross-market correlation |
| `anomaly_probability` | Statistical outlier score across the account population |
| `stddevs_above_expected` | Z-score of wins vs. expected baseline |

---

## What is implemented vs. planned

### Implemented
- FastAPI app with all signal routes — all return **real computed data** from JSONL or Postgres
- HTML landing page served from root
- Prometheus metrics endpoint
- CI pipeline (lint, type check, test)
- Railway deployment config (Railpack, health check, restart policy)
- Ingestor CLI with Kalshi HTTP client (RSA keypair auth, pagination, retry backoff)
- **Postgres schema + Alembic migrations** (`services/ingestor/alembic/`)
- **Dual-path storage**: JSONL (dev/default) or Postgres (when `DATABASE_URL` is set) for both ingestor writes and API reads
- **Account enrichment computation** from fills data — `insider_like_score` is now non-zero
- **Structured logging** in ingestor (replaces all `print()`)
- **Startup validation** — ingestor fails fast if Kalshi auth or tickers are missing
- Shared `_paths.py` module (eliminates 4 copies of path-resolution logic in API)
- `.env.example` documenting every env var

### Pending / in progress
- Redis + Celery async job queue
- Full OpenTelemetry trace propagation
- Local dev runbook (docs/runbook.md is TBD)
- Postgres read path for enrichment when running in DB mode (enrichment computed from JSONL even in Postgres mode — tracked as a future improvement)

---

## Deployment

Deployed on **Railway** via Railpack builder.

- Single process defined in `Procfile`: `web: ./scripts/start-api.sh`
- `scripts/start-api.sh` sets `PYTHONPATH=apps/api/src` and starts uvicorn on `$PORT`
- Health check: `GET /health`, 60s initial delay, 10 restart retries
- `railway.toml` controls builder and health check config

---

## Conventions

- **One router per route file** under `apps/api/src/marketsignalos_api/api/routes/`
- **Services live in** `apps/api/src/marketsignalos_api/services/` — no business logic in route files
- **Pydantic models** for all request/response schemas
- **No ORM yet** — raw SQL via `asyncpg` or `psycopg` when the DB layer is wired
- **No `print()`** — use the stdlib `logging` module with structured output
- **TimescaleDB hypertables** for any time-series table (trades, metrics); regular tables for reference data (accounts, markets)
- **Frontend** uses `force-dynamic` rendering; all data fetched server-side from the API via `NEXT_PUBLIC_API_BASE_URL`

---

## Docs

| File | Contents |
|---|---|
| `docs/prd.md` | Product requirements, MVP scope, success metrics |
| `docs/architecture.md` | High-level data flow and key principles |
| `docs/0001-tech-stack.md` | ADR explaining stack choices |
| `docs/runbook.md` | Operational runbook (WIP) |
| `docs/kalshi-ingestion-faq.md` | Kalshi-specific gotchas |
| `docs/fix-lessons-learned.md` | Post-mortems and lessons |
