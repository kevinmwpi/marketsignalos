# MarketSignalOS

Identifies **skilled Polymarket wallets** by their on-chain win rate, surfaces
the positions they're currently holding, and — for each one — provides a deep
link to the equivalent Kalshi market so US-based users can tail the bet on
Kalshi.

Kalshi's per-user history is not public, so wallet skill is computed entirely
from Polymarket's on-chain data; Kalshi's role is the *tail target* where the
operator actually places the bet. See [`docs/0002-cross-exchange-decision.md`](docs/0002-cross-exchange-decision.md)
for the design ADR (including the 2026-05-23 correction that reversed the
short-lived "cross-exchange dislocation" framing) and [`CLAUDE.md`](CLAUDE.md)
for the full developer guide.

## What you get

- **Dashboard at `/`** — feed of currently-held BUY positions from skilled Polymarket wallets, each row carrying a Kalshi mirror link when a match exists, plus a leaderboard sidebar of skilled wallets
- **Run ingest button** — one click runs the entire pipeline end-to-end (Polymarket leaderboards → wallet activity → enrichment → Kalshi market fetch → Polymarket→Kalshi matcher) using only public, unauthenticated APIs. No keys required.

## Repository layout

- `apps/api/` — FastAPI backend (signal computation + REST endpoints)
- `apps/web/` — Next.js 16 frontend
- `services/polymarket-ingestor/` — Polymarket ingestion + Polymarket→Kalshi market matcher
- `services/ingestor/data/` — shared JSONL data directory (gitignored)
- `docs/` — product, architecture, ADRs

## Quick start

```powershell
# Python deps
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
.\.venv\Scripts\pip install -e "services/polymarket-ingestor[dev]"

# Run the API
$env:PYTHONPATH = "apps/api/src"
.\.venv\Scripts\uvicorn marketsignalos_api.main:app --reload --port 8000

# Run the web app (separate terminal)
cd apps/web
npm ci
$env:NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000"
npm run dev   # http://localhost:3000
```

Open `http://localhost:3000` and click **Run ingest** in the top nav. No env
vars are required; the pipeline uses public Polymarket and Kalshi endpoints.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q apps/api services/polymarket-ingestor

cd apps/web
npm run lint
npm run build
```

## Deploy API on Railway

Single web process is defined in `Procfile` (`web: ./scripts/start-api.sh`).
`railway.toml` selects Railpack and pins `/health` as the healthcheck. The
service needs no required env vars; optional `DATABASE_URL` enables Postgres
dual-write and `FRONTEND_URL` adds an "Open dashboard" button to the API's
landing page. See [`CLAUDE.md`](CLAUDE.md) for the full env-var table.
