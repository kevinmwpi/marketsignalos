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
- `GET /signals/suspicious-accounts?fresh_days=30&min_resolved=20&baseline_win_rate=0.5&sigma_threshold=3&only_significant=true&limit=100` returns statistically significant winning accounts from ingested fills + market resolutions

## Run tests

From `apps/api`:

```powershell
pytest -q
```
