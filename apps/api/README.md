# API (FastAPI)

## Run locally

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
cd apps/api
pip install -e ".[dev]"
```

Start the API from `apps/api`:

```powershell
uvicorn marketsignalos_api.main:app --reload
```

## Endpoints

- `GET /health` returns `{"status":"ok"}`
- `GET /metrics` returns Prometheus metrics text format
- `GET /signals/trades?limit=50` returns latest ingested normalized trades from local store
- `GET /signals/leaderboard?fresh_days=30&min_resolved=20&limit=50` returns ranked accounts based on skill-vs-luck scoring and insider-like enrichment

## Run tests

From `apps/api`:

```powershell
pytest -q
```
