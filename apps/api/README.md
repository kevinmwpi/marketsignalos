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

- `GET /` serves the Railway-friendly landing page with a leaderboard table and quick links to docs/health
- `GET /health` returns `{"status":"ok"}`
- `GET /metrics` returns Prometheus metrics text format
- `GET /signals/trades?limit=50` returns latest ingested normalized trades from local store
- `GET /signals/leaderboard?fresh_days=30&min_resolved=20&limit=50` returns ranked accounts based on skill-vs-luck scoring and insider-like enrichment
- `GET /signals/orderflow?limit=50&min_odds_jump=10&min_size_zscore=2.5&min_large_quantity=100` flags unusual orderflow events such as sudden odds moves and outsized bets

## Run tests

From `apps/api`:

```bash
pytest -q
```

If you prefer to run tests from the repository root instead, use:

```bash
python -m pytest -q apps/api/tests
```
