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

- `requirements.txt` installs the API package from `apps/api` (`-e ./apps/api`)
- `Procfile` declares the web process command for Uvicorn
- `railway.toml` sets an explicit deploy `startCommand`

In Railway:

1. Create a new service from this repository.
2. Keep the root as the service source directory.
3. Set any required runtime variables (for example, API keys) in Railway Variables.
4. Deploy — Railway will use the root `requirements.txt` during build and the explicit start command during deploy.

## API endpoints

- `GET /health`
- `GET /metrics`
- `GET /signals/trades?limit=50`
- `GET /signals/leaderboard?fresh_days=30&min_resolved=20&limit=50`

## Tests

API tests:

```powershell
cd apps/api
..\..\.venv\Scripts\python.exe -m pytest -q
```

