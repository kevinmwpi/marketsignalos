# CLAUDE.md — MarketSignalOS

## What this project is

MarketSignalOS identifies skilled Polymarket wallets, surfaces their currently-held positions, and classifies each bet by where it can actually be tailed (Polymarket first; approved Kalshi mirror as fallback).

**Core value:** scan Polymarket leaderboards + skill-qualified wallets → score by Bayesian forecast edge → show actionable open BUY positions with tradability classification (`poly_direct`, `kalshi_mirror`, `on_chain_only`, `closed`).

**Why not Kalshi-as-source.** Kalshi hides per-user bet history, so there's no way to compute a true skill score from public Kalshi data. The original Kalshi-only design was abandoned for this reason; only Kalshi's *public market list* is consumed (to find an optional Kalshi mirror when Polymarket is unavailable). See `docs/0002-cross-exchange-decision.md`.

**Execution model (2026-06 update).** Polymarket is the primary tail venue when Gamma marks a market active. Kalshi appears only for **approved** mirrors when Polymarket is closed/unavailable. On-chain-only signals (common for international/weather markets) are hidden from the default feed.

**Non-goals (never build these):**
- Automated trading or order placement
- Claims of insider detection or illegal activity
- ML-first models in v1 (deterministic/explainable only)
- Kafka or event-streaming infrastructure
- Anything that requires a Kalshi user account or scraping per-user Kalshi history

---

## Repository layout

```
marketsignalOS/
├── apps/
│   ├── api/                       FastAPI backend — signal computation + REST endpoints
│   └── web/                       Next.js 16 frontend dashboard
├── services/
│   ├── ingestor/data/             Shared JSONL data dir (gitignored; only artifact in this tree)
│   └── polymarket-ingestor/       Polymarket ingestion + Polymarket→Kalshi market matcher
├── docs/                          Architecture, PRD, ADRs, runbooks
├── scripts/                       Deployment entry points + discovery probes
├── .github/workflows/ci.yml       Python (lint/type/test) + Node (lint/build)
├── pyproject.toml                 Root Python tooling config (ruff, mypy, pytest paths)
├── railway.toml                   Railway deployment config
└── Procfile                       Single process: web → scripts/start-api.sh
```

---

## Dev setup

**Prerequisites:** Python 3.12, Node 20 (`.nvmrc`). No Kalshi credentials required. Postgres is optional (`DATABASE_URL` opt-in).

```powershell
# Python — create a shared venv at repo root
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
.\.venv\Scripts\pip install -e "services/polymarket-ingestor[dev]"

# Node (web dashboard)
cd apps/web
npm ci
```

**Environment variables (all optional):**

| Variable | Where used | Notes |
|---|---|---|
| `PORT` | API server | Defaults to `8080`; Railway injects this automatically |
| `NEXT_PUBLIC_API_BASE_URL` | Next.js frontend | e.g. `http://localhost:8000` |
| `FRONTEND_URL` | API | Shown on the API's `/` landing page as "Open dashboard" |
| `DATABASE_URL` | API + Polymarket ingestor | Postgres connection string. When set, the ingestor dual-writes JSONL + Postgres |
| `POLYMARKET_DATA_DIR` | Polymarket ingestor + API | Override JSONL directory (defaults to `services/ingestor/data/`) |
| `POLYMARKET_WATCHLIST_PATH` | Polymarket ingestor | Wallet watchlist file (auto-seeded each pipeline run) |
| `INGEST_POLYMARKET` | Polymarket ingestor | Kill switch for the legacy `all` CLI subcommand. The web button bypasses this |

---

## Running things locally

```powershell
# API (from repo root)
$env:PYTHONPATH = "apps/api/src;services/polymarket-ingestor/src"
.\.venv\Scripts\uvicorn marketsignalos_api.main:app --reload --port 8000

# Web dev server
cd apps/web
npm run dev   # http://localhost:3000

# Polymarket pipeline CLI — same orchestrator the web "Run ingest" button uses.
# Subcommands: pipeline (recommended), seed-watchlist, wallets, markets,
# enrichment, fetch-kalshi-markets, match-markets, review-matches, all, leaderboard.
.\.venv\Scripts\marketsignalos-polymarket-ingestor pipeline
```

---

## Testing

```powershell
# All Python tests from repo root
.\.venv\Scripts\python.exe -m pytest -q apps/api services/polymarket-ingestor

# Web
cd apps/web
npm run lint
npm run build
```

CI runs Python 3.12 on ubuntu-latest (ruff → mypy → pytest) and Node 20 (lint → build) on every PR. Both must pass before merging.

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
   Polymarket public APIs                                Kalshi /markets (public)
   (leaderboards + activity + Gamma)                              ↓
              ↓                                          kalshi_markets.jsonl
   Polymarket pipeline (services/polymarket-ingestor/runner.py:run_pipeline)
              ↓
   polymarket_leaderboard.jsonl    polymarket_activity.jsonl   polymarket_positions.jsonl   polymarket_markets.jsonl
              ↓                              ↓
   skill_computation.py → polymarket_wallet_enrichment.jsonl
              ↓                              ↓
              └────→ market_matcher.py ←──── kalshi_markets.jsonl
                         ↓
                  market_links.jsonl ──→ skilled_bets.py service
                                              ↓
                  FastAPI endpoints (apps/api/src/marketsignalos_api/api/routes/)
                         ↓
                  Next.js dashboard (apps/web): `/`
```

The pipeline runs JSONL-first. Postgres is opt-in via `DATABASE_URL` (`Dual*` stores fan every write out to JSONL **and** Postgres). API reads always come from JSONL.

### Key API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Minimal HTML landing page (orientation only; dashboard is the Next.js app) |
| `GET` | `/health` | Health check (used by Railway) |
| `GET` | `/metrics` | Prometheus text exposition |
| `POST` | `/ingestor/run` | Triggers one pass of `run_pipeline()`; returns 202 + started_at |
| `GET` | `/ingestor/status` | Live state: running flag, last_exit_code, last_error, log_tail, last_summary |
| `GET` | `/signals/skilled-bets` | Still-held BUY entries by skilled wallets, joined with the Kalshi mirror (headline endpoint — drives the `/` dashboard). Fresh entries rank above priced-in ("late") ones |
| `GET` | `/signals/polymarket-leaderboard` | Skilled wallets ranked by on-chain win rate |
| `GET` | `/signals/ledger` | Paper-trade ledger: every surfaced signal with its surface-time prices and settlement status |
| `GET` | `/signals/ledger/summary` | Hit rate + hypothetical $1-per-signal tail ROI (at surface price and at wallet entry) |
| `POST` | `/signals/ledger/refresh` | Record newly surfaced signals + settle resolved ones (runs automatically after API-triggered ingests) |
| `GET` | `/docs` | Auto-generated Swagger UI |

---

## Signal design principles

- **Deterministic first.** Every score must be reproducible from the same input data. No model weights, no stochastic components in v1.
- **Explainable drivers.** Every top-level score (skill likelihood, win rate, match confidence) decomposes into human-readable sub-signals.
- **Non-accusatory language.** "skill likelihood", "edge", "match confidence" — never "insider trading" or "manipulation".
- **Freshness is a product feature.** Every surface carries the timestamp of the underlying snapshot.

### Core scoring concepts

| Signal | Description |
|---|---|
| `skill_likelihood` | Bayesian forecast edge: P(edge > 0 \| data) vs market-implied entry prices |
| `independent_settled_events` | Effective sample size penalizing correlated bets within the same event |
| `recent_*` (per wallet) | Recency-weighted edge fit (180-day half-life, anchored to the newest bet in the dataset). Wallets with <5 recent independent events, or a negative recent edge, lose tailability |
| `tradability` (per bet) | `poly_direct` \| `kalshi_mirror` \| `on_chain_only` \| `closed` — execution path classification |
| `move_captured_pct` (per bet) | Fraction of the entry→$1 move already made: `(current − entry) / (1 − entry)`. Drives `remaining_edge_status` (`discounted`/`fresh`/`partial`/`late`/`unknown`) and feed ordering |
| `kalshi_match_confidence` | TF-IDF cosine similarity between Polymarket and Kalshi market titles (±3-day date window) |

---

## What is implemented vs. planned

### Implemented
- FastAPI app: `/` (minimal HTML landing), `/health`, `/metrics`, `/ingestor/{run,status}`, `/signals/skilled-bets`, `/signals/polymarket-leaderboard`
- **`run_pipeline()`** — single in-process orchestrator the web "Run ingest" button invokes. Seeds wallets across `day/week/month/all` windows (gracefully skipping any window the API rejects), pulls activity/positions/value, fetches Polymarket markets + Kalshi public markets, and runs the Polymarket→Kalshi market matcher. No env vars required.
- **`/signals/skilled-bets`** — still-held BUY entries from wallets with `skill_likelihood ≥ 0.8`, each row carrying the Kalshi mirror (ticker, title, deep link, live YES price, match confidence) when a match exists
- **SkilledBetsPanel + PolymarketLeaderboardPanel + IngestButton** mounted on `/` (the dashboard root)
- **Ingest button** — pre-flight-free (no required env vars); log capture surfaces a `log_tail` and counts summary back to the UI
- **Polymarket Postgres write path** — Alembic schema (`services/polymarket-ingestor/alembic/`) covers 8 tables; `Dual*` store wrappers fan every write out to JSONL and Postgres when `DATABASE_URL` is set; API reads remain JSONL-only
- **Kalshi parlay-ticker filter** — `_is_kalshi_parlay()` excludes `KXMVE*` multi-leg tickers from the matcher
- **Recency-weighted edge (`forecast-v3`)** — a second Bayesian fit with each bet's likelihood weight decayed at a 180-day half-life; `recent_*` enrichment fields plus two new tailability gates (recent independent events ≥ 5; recent edge not negative)
- **Remaining-edge gate** — each feed row carries `move_captured_pct` + `remaining_edge_status`; the feed sorts fresh/discounted entries above partial ones with late last, and `max_move_captured` can drop converged signals entirely
- **Signal outcome ledger** — `signal_ledger.jsonl` records every surfaced signal once (at its surface-time prices) and settles it when the market resolves; `/signals/ledger*` endpoints expose rows, hit rate, and hypothetical tail ROI

### Pending / in progress
- `/positions` pagination (currently caps at ~100 per wallet)
- Embedding-based matcher (only if TF-IDF precision proves inadequate)
- Full OpenTelemetry trace propagation
- Runbook (`docs/runbook.md`)

---

## Deployment

Deployed on **Railway** via Railpack builder.

- Single process defined in `Procfile`: `web: ./scripts/start-api.sh`
- `scripts/start-api.sh` sets `PYTHONPATH=apps/api/src` and starts uvicorn on `$PORT`
- Health check: `GET /health`, 60s initial delay, 10 restart retries
- `railway.toml` controls builder + health check config
- The Next.js app is a separate deployment (Vercel or a second Railway service). Set `NEXT_PUBLIC_API_BASE_URL` on the frontend to the API URL; optionally set `FRONTEND_URL` on the API so its landing page links back.

---

## Conventions

- **One router per route file** under `apps/api/src/marketsignalos_api/api/routes/`
- **Services live in** `apps/api/src/marketsignalos_api/services/` — no business logic in route files
- **Pydantic models** for all request/response schemas
- **No `print()`** — use the stdlib `logging` module with structured output
- **Frontend** uses `force-dynamic` rendering; all data fetched server-side from the API via `NEXT_PUBLIC_API_BASE_URL`

---

## Docs

| File | Contents |
|---|---|
| `docs/prd.md` | Product requirements, MVP scope, success metrics |
| `docs/architecture.md` | High-level data flow and key principles |
| `docs/0001-tech-stack.md` | ADR explaining stack choices |
| `docs/0002-cross-exchange-decision.md` | ADR for the Polymarket pivot (with the 2026-05-23 correction reversing the cross-exchange framing) |
| `docs/polymarket-pipeline.md` | Full Polymarket data-flow reference |
| `docs/polymarket-api-discovery.md` | Validated public Polymarket endpoint shapes |
| `docs/runbook.md` | Operational runbook (WIP) |
| `docs/fix-lessons-learned.md` | Post-mortems and lessons |
