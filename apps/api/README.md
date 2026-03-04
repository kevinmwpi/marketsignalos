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

## Run tests

From `apps/api`:

```powershell
pytest -q
```
