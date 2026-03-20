# MarketSignalOS

MarketSignalOS ingests prediction-market data and ranks accounts by statistical skill signals rather than raw PnL popularity.

## Current scope

- Kalshi data ingestion in `services/ingestor`
- FastAPI backend in `apps/api`
- Next.js leaderboard UI in `apps/web`

## Repository layout

- `apps/api`: API routes and scoring services
- `apps/web`: dashboard frontend
- `services/ingestor`: market data ingestion pipeline
- `docs`: product and architecture notes

## Quick start

From repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e "apps/api[dev]"
```

Run API:

```powershell
cd apps/api
uvicorn marketsignalos_api.main:app --reload
```

Run web:

```powershell
cd apps/web
npm ci
$env:NEXT_PUBLIC_API_BASE_URL="http://localhost:8000"
npm run dev
```


## Deploy API on Railway

This repository is a monorepo, so Railway needs explicit root-level app metadata for the API service.

Railway config in this repo:

- `requirements.txt` declares direct runtime dependencies (`fastapi`, `prometheus-client`, `uvicorn`)
- `Procfile` declares the web process command for Uvicorn
- `railway.toml` selects Railpack for the root deploy and sets the explicit start command
- `nixpacks.toml` remains in the repo as an alternative Python-first build plan if the service is switched back to Nixpacks
- `Procfile`/`railway.toml` use `env PYTHONPATH=apps/api/src` and `${PORT:-8000}` so the API module is importable in this monorepo layout and Uvicorn always receives a concrete port value

In Railway:

1. Create a new service from this repository.
2. Keep the root as the service source directory.
3. Set any required runtime variables (for example, API keys) in Railway Variables.
4. Deploy — Railway will install from root `requirements.txt` and start the API with the explicit `PYTHONPATH` + Uvicorn command.
5. Opening the Railway service root URL now shows a lightweight leaderboard landing page served by FastAPI. Interactive API docs remain available at `/docs`.

## API endpoints

- `GET /` (serves the MarketSignalOS landing page and leaderboard frontend shell)
- `GET /health`
- `GET /metrics`
- `GET /signals/trades?limit=50`
- `GET /signals/leaderboard?fresh_days=30&min_resolved=20&limit=50`

## Tests

API tests:

```bash
python -m pytest -q apps/api/tests
```
