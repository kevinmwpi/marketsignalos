# MarketSignalOS

MarketSignalOS identifies **skilled Polymarket wallets** from public wallet
history, verifies that their edge is economically meaningful, and surfaces the
BUY positions they still hold. For each tailable signal, the dashboard links to
the Polymarket source market and, when a reviewed match exists, to the
equivalent Kalshi market so US-based users can tail the idea on Kalshi.

Kalshi's per-user history is not public, so wallet skill is computed entirely
from Polymarket's public on-chain/data APIs. Kalshi is the *tail target*, not a
second leg in a cross-exchange spread. See
[`docs/0002-cross-exchange-decision.md`](docs/0002-cross-exchange-decision.md)
for the design ADR and [`CLAUDE.md`](CLAUDE.md) for the full developer guide.

## What you get

- **Dashboard at `/`** — a feed of currently-held BUY positions from trusted,
  tailable Polymarket wallets, including entry price/size, current price drift,
  still-held exposure, wallet provenance, data-quality badges, and a Kalshi
  mirror call-to-action when a match exists.
- **Skilled wallet leaderboard** — ranks wallets by the newer conservative
  forecast-edge score instead of raw win rate alone, with tailable/blocked
  status, economics, and independent settled-event counts.
- **Shallow and deep ingest controls** — the web app exposes a fast refresh for
  the current watchlist and a deeper rebuild that sweeps categorized
  leaderboards, discovers recent traders, hydrates complete wallet history,
  prunes dormant wallets, refreshes markets/enrichment, and updates Kalshi
  mirrors.
- **Trust and economics gates** — signals are quarantined until the wallet has
  complete activity/position/closed-position coverage, market metadata coverage,
  positive economics, and a `forecast-v2` score.
- **JSONL-first storage with optional Postgres dual-write** — local development
  works without a database, while `DATABASE_URL` enables the ingestor to mirror
  writes into Postgres using the bundled Alembic schema.

## Repository layout

- `apps/api/` — FastAPI backend for signal computation, ingest triggers, health,
  and REST endpoints.
- `apps/web/` — Next.js 16 dashboard.
- `services/polymarket-ingestor/` — Polymarket ingestion, wallet hydration,
  Bayesian skill scoring, Postgres/JSONL stores, and Polymarket→Kalshi matcher.
- `services/ingestor/data/` — default shared JSONL data directory (gitignored).
- `docs/` — product, architecture, ADRs, runbooks, and pipeline references.

## Quick start

```powershell
# Python 3.12+ is required.

# Python deps
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
.\.venv\Scripts\pip install -e "services/polymarket-ingestor[dev]"

# Run the API
$env:PYTHONPATH = "apps/api/src;services/polymarket-ingestor/src"
.\.venv\Scripts\uvicorn marketsignalos_api.main:app --reload --port 8000

# Run the web app (separate terminal)
cd apps/web
npm ci
$env:NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000"
npm run dev   # http://localhost:3000
```

Open `http://localhost:3000` and use the top-nav ingest controls:

- **Run shallow** refreshes the current watchlist, wallet activity/positions,
  market metadata, enrichment, Kalshi markets, and matches.
- **Run deep** performs the broader research rebuild and may take much longer:
  categorized leaderboard sweep, recent-trader discovery, wallet pruning,
  batched hydration, then the same market/enrichment/Kalshi refresh.

No API keys are required; the pipeline uses public Polymarket and Kalshi
endpoints. Optional `DATABASE_URL` enables Postgres dual-write.

## CLI ingest workflows

The `marketsignalos-polymarket-ingestor` console script is installed from
`services/polymarket-ingestor`.

```powershell
# Fast watchlist refresh, equivalent to the dashboard's shallow run.
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe pipeline

# Deep research rebuild. Tune batch size/pages for the environment.
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe deep-pipeline --wallet-batch-size 25

# Legacy one-pass mode remains guarded by a kill switch.
$env:INGEST_POLYMARKET = "1"
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe all
```

Useful individual subcommands include `seed-watchlist`, `wallets`, `markets`,
`enrichment`, `fetch-kalshi-markets`, `match-markets`, `review-matches`,
`recent-traders`, `prune-wallets`, `pin-wallet`, and `unpin-wallet`. See
[`docs/polymarket-pipeline.md`](docs/polymarket-pipeline.md) for the full data
flow and run order.

## API surface

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | API liveness. |
| `GET` | `/ingestor/status` | Current/last ingest state, progress, log tail, and last summary. |
| `POST` | `/ingestor/run` | Starts the shallow Polymarket pipeline. |
| `POST` | `/ingestor/run/deep` | Starts the deep research rebuild. |
| `GET` | `/signals/skilled-bets?min_skill=X&min_resolved=N&min_position_value_usdc=V&limit=N` | Still-held BUY positions from trusted, tailable skilled wallets, newest first. |
| `GET` | `/signals/skilled-bets/summary` | Trust/rebuild counts used by the dashboard banner. |
| `GET` | `/signals/polymarket-leaderboard?min_resolved=N&min_skill=X&tailability=all|tailable|blocked&limit=N` | Ranked Polymarket wallets with forecast edge, economics, quality, and tailability fields. |

## Scoring and signal semantics

`skill_computation.py` now uses a Bayesian forecast-edge model that compares a
wallet's resolved outcomes against each market's own implied probability, not
just a naive 50/50 win-rate baseline. The enrichment output keeps legacy fields
for API compatibility while adding:

- posterior edge metrics (`forecast_skill_likelihood`, `forecast_edge_mean`,
  `forecast_edge_lower_bound`, `independent_settled_events`);
- economics (`all_time_pnl_usdc`, `all_time_volume_usdc`, `all_time_roi`,
  `pnl_30d_usdc`, `active_pnl_usdc`, `max_drawdown_usdc`);
- trust gates (`data_quality_status`, `data_quality_reasons`,
  `economic_qualified`, `tailability_status`, `tailability_reasons`,
  `score_version`).

The skilled-bets feed only admits wallets with trusted `forecast-v2` enrichment
and `tailability_status="tailable"`, then joins the latest complete position
snapshot with the latest BUY activity, market metadata, and the best
non-rejected Kalshi mirror.

## Storage and migrations

Local JSONL files remain the source of truth for development and demos. Set
`POLYMARKET_DATA_DIR` to override the default `services/ingestor/data/` folder.
When `DATABASE_URL` is present, the ingestor wraps JSONL stores with matching
Postgres stores and dual-writes leaderboard rows, activity, complete position
snapshots, closed positions, wallet values, hydration state, market metadata,
market links, and enrichment.

Run Postgres migrations from the ingestor package:

```powershell
cd services/polymarket-ingestor
$env:DATABASE_URL = "postgresql://user:pass@host:5432/dbname"
alembic upgrade head
```

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
service needs no required env vars. Optional `DATABASE_URL` enables Postgres
dual-write/readiness for the ingestor path, and `FRONTEND_URL` adds an "Open
dashboard" button to the API's landing page. See [`CLAUDE.md`](CLAUDE.md) for
the full env-var table.
