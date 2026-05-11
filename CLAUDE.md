# CLAUDE.md — MarketSignalOS

## What this project is

MarketSignalOS is a cross-exchange prediction-market signal system. The primary signal is **tradeable price dislocation**: skilled Polymarket wallets' active positions priced against equivalent Kalshi markets. Polymarket on-chain trade history (publicly queryable per wallet on Polygon) is the source of skill scoring; Kalshi is the comparison side.

**Core value:** Find wallets with a defensible on-chain win rate, identify their currently-open positions, and surface the corresponding Kalshi market when it's mispriced relative to Polymarket.

The original Kalshi-only design was abandoned because Kalshi hides per-user bet history — there's no way to compute a true win rate from public Kalshi data. See `docs/0002-cross-exchange-decision.md` for the strategic ADR and `docs/polymarket-pipeline.md` for the full data-flow reference. The Kalshi pipeline is retained and feeds the sidebar leaderboard / orderflow surfaces.

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
│   ├── api/                       FastAPI backend — signal computation + REST endpoints
│   └── web/                       Next.js 16 frontend dashboard
├── services/
│   ├── ingestor/                  Kalshi data ingestion workers
│   └── polymarket-ingestor/       Polymarket on-chain + matcher pipeline
├── docs/                          Architecture, PRD, ADRs, runbooks
├── scripts/                       Deployment entry points + discovery probes
├── .github/workflows/             CI — Python (lint/type/test) + Node (lint/build)
├── pyproject.toml                 Root Python tooling config (ruff, mypy, pytest paths)
├── railway.toml                   Railway deployment config
└── Procfile                       Single process: web → scripts/start-api.sh
```

---

## Dev setup

**Prerequisites:** Python 3.12, Node 20 (`.nvmrc`), a Postgres + TimescaleDB instance.

```powershell
# Python — create a shared venv at repo root
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
.\.venv\Scripts\pip install -e "services/ingestor[dev]"
.\.venv\Scripts\pip install -e "services/polymarket-ingestor[dev]"

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
| `KALSHI_API_KEY` | Ingestor | Kalshi bearer auth token |
| `INGEST_AUTO_DISCOVER` | Ingestor | Set to `1` to auto-discover active markets |
| `INGEST_SCRAPE_LEADERBOARD` | Ingestor | Set to `1` to scrape Kalshi leaderboard via Playwright |
| `KALSHI_WATCHLIST_PATH` | Ingestor | Path to manual watchlist JSONL |
| `KALSHI_PROFILE_SNAPSHOTS_PATH` | Ingestor + API | Path to scraped profile snapshots JSONL |
| `POLYMARKET_DATA_DIR` | Polymarket ingestor + API | Override JSONL directory (defaults to shared `services/ingestor/data/`) |
| `POLYMARKET_WATCHLIST_PATH` | Polymarket ingestor | Wallet watchlist file (auto-seeded from leaderboard) |
| `INGEST_POLYMARKET` | Polymarket ingestor | Kill switch for `all` mode — must be `1`/`true`/`yes`/`on` to enable |

---

## Running things locally

```powershell
# API (from repo root)
$env:PYTHONPATH = "apps/api/src"
.\.venv\Scripts\uvicorn marketsignalos_api.main:app --reload --port 8000

# Web dev server
cd apps/web
npm run dev   # http://localhost:3000

# Kalshi ingestor CLI
.\.venv\Scripts\marketsignalos-ingestor

# Polymarket ingestor CLI — subcommands: seed-watchlist, wallets, markets,
# enrichment, fetch-kalshi-markets, match-markets, review-matches, all.
# `all` requires INGEST_POLYMARKET=1.
.\.venv\Scripts\marketsignalos-polymarket-ingestor seed-watchlist --top-n-profit 50
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

# Polymarket ingestor tests
cd services/polymarket-ingestor
..\..\  .venv\Scripts\python.exe -m pytest -q

# All Python tests from repo root
.\.venv\Scripts\python.exe -m pytest -q apps/api services/ingestor services/polymarket-ingestor

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
Polymarket (Data API + Gamma + Goldsky)              Kalshi (public /markets)
  → Polymarket ingestor (services/polymarket-ingestor)  → Kalshi ingestor
        ↓                                                       ↓
  polymarket_activity.jsonl                               kalshi_*.jsonl
  polymarket_positions.jsonl                                    ↓
  polymarket_markets.jsonl                                  (signal engine)
        ↓                                                       ↓
  skill_computation.py → polymarket_wallet_enrichment.jsonl    ↓
        ↓                                                       ↓
        └────→  market_matcher.py  ←──── kalshi_markets.jsonl  ←┘
                     ↓
              market_links.jsonl
                     ↓
              cross_exchange.py service
                     ↓
              FastAPI endpoints (apps/api/api/routes/)
                     ↓
              Next.js dashboard (apps/web)
```

The Polymarket pipeline runs JSONL-first; Postgres support is stubbed. See `docs/polymarket-pipeline.md` for the full file inventory and run order.

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
| `GET` | `/signals/polymarket-leaderboard` | Skilled Polymarket wallets (on-chain win rate) |
| `GET` | `/signals/cross-exchange` | Tradeable Kalshi ↔ Polymarket price dislocations |
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
- HTML landing page served from root + Prometheus metrics endpoint
- CI pipeline (lint, type check, test) for `apps/api`, `services/polymarket-ingestor`, and `apps/web`
- Railway deployment config (Railpack, health check, restart policy)
- Kalshi ingestor with HTTP client (RSA keypair auth, pagination, retry backoff)
- Kalshi Postgres schema + Alembic migrations (`services/ingestor/alembic/`)
- Kalshi dual-path storage: JSONL (default) or Postgres when `DATABASE_URL` is set
- **Polymarket ingestor** (`services/polymarket-ingestor/`) with full data flow: leaderboard seeding → wallet activity/positions (paginated + checkpointed) → markets → on-chain skill computation → cross-exchange matcher → market_links upsert with sticky manual overrides
- **`/signals/polymarket-leaderboard`** — skilled wallets by on-chain win rate
- **`/signals/cross-exchange`** — the marquee signal: tradeable Kalshi↔Polymarket price dislocations weighted by wallet skill
- **Dashboard panels** — `CrossExchangePanel` (full-width marquee) and `PolymarketLeaderboardPanel` (sidebar) wired into `apps/web/app/page.tsx`
- **`INGEST_POLYMARKET` kill switch** — `all` mode no-ops unless set, so the package can deploy without triggering load

### Pending / in progress
- Polymarket Postgres migrations (Phase 7.5; stubs exist in `services/polymarket-ingestor/.../storage.py`)
- Kalshi parlay-ticker filtering for the matcher (multi-leg `KXMVE*` titles pollute TF-IDF)
- `/positions` endpoint pagination (currently caps at ~100 per wallet)
- Embedding-based matcher (only if TF-IDF precision/recall proves inadequate)
- Redis + Celery async job queue
- Full OpenTelemetry trace propagation
- Local dev runbook (docs/runbook.md is TBD)

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
| `docs/0002-cross-exchange-decision.md` | ADR for the Polymarket pivot + cross-exchange product direction |
| `docs/polymarket-pipeline.md` | Full Polymarket data-flow reference (files, env vars, CLI subcommands, run order) |
| `docs/polymarket-phase-status.md` | Phase-by-phase build log for the Polymarket pivot |
| `docs/polymarket-api-discovery.md` | Validated public Polymarket endpoint shapes (Phase 0 output) |
| `docs/runbook.md` | Operational runbook (WIP) |
| `docs/kalshi-ingestion-faq.md` | Kalshi-specific gotchas |
| `docs/fix-lessons-learned.md` | Post-mortems and lessons |
