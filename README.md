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

