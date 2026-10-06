# CLAUDE.md — MarketSignalOS

**Current state (2026-09-30):** the public dashboard is a static site on GitHub
Pages that reads a research snapshot collected by a scheduled GitHub Actions job.
The lean-pilot worker runs as an hourly Railway cron service (`railway.toml`,
`deploy/`). The FastAPI app is not deployed; operator routes require
`ADMIN_API_TOKEN`, and local development may opt in to
`ALLOW_UNAUTHENTICATED_ADMIN=1` plus `VITE_SHOW_INGEST_CONTROLS=1`.

> **Read [`docs/handoff-blueprint.md`](docs/handoff-blueprint.md) before starting any
> build work.** It is the authoritative staged plan: what to build in what order,
> the entry gate and acceptance evidence for each stage, the invariants that hold
> everywhere, and the kill criteria. It supersedes any older architecture plan.

## What this project is

Current delivery constraint: target $15/month on Railway Hobby. Prepare and
review a concrete deployment before paid provisioning. After each development
pass, summarize what was built, validation, remaining work, and cloud activation
status. See `docs/lean-pilot.md` for implemented controls and known limitations.
See `docs/metadata-backfill.md` for bounded Gamma request receipts and the isolated
candidate-coverage probe; present-day observations must not enter frozen scores.

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
marketsignalos/
├── apps/
│   ├── api/                       FastAPI backend: signal computation + REST endpoints
│   └── dashboard/                 React/Vite dashboard (static build on GitHub Pages)
├── services/
│   ├── ingestor/data/             Default JSONL data dir (gitignored)
│   └── polymarket-ingestor/       Ingestion, skill scoring, lean pilot, market matcher
├── deploy/                        Worker image, entry point and pilot config
├── docs/                          Blueprint, evidence (docs/benchmarks), ADRs, runbooks
├── evals/market_matching/         Hand-labelled matcher eval set
├── ops/prometheus/alerts.yml      Alerting rules (incident regressions + SLOs)
├── ops/grafana/                   Alloy scrape config + dashboard + Grafana Cloud runbook
├── scripts/                       Research collection, live probes and their tests
├── .github/workflows/             CI, Pages publishing, worker image, scheduled snapshot
├── pyproject.toml / uv.lock       Python workspace (uv) and tooling config
├── package.json / pnpm-*.yaml     Dashboard workspace (pnpm)
└── railway.toml                   Railway config for the lean-pilot worker
```

---

## Dev setup

**Prerequisites:** Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 24 with
pnpm 10. No Kalshi credentials required. Postgres is optional (`DATABASE_URL` opt-in).

```sh
uv sync --frozen                 # Python: both packages, editable, plus dev tools
pnpm install --frozen-lockfile   # Dashboard
```

**Environment variables (production requirements in the deployment guide):**

| Variable | Where used | Notes |
|---|---|---|
| `PORT` | API server | Defaults to `8080`; Railway injects this automatically |
| `VITE_API_BASE_URL` | Dashboard build | API origin, e.g. `http://localhost:8000`; defaults to same-origin `/api` |
| `VITE_STATIC_SITE` | Dashboard build | `1` builds the static site: no API requests, API-backed panels replaced by a notice |
| `FRONTEND_URL` | API | Shown on the API's `/` landing page as "Open dashboard" |
| `DATABASE_URL` | API + Polymarket ingestor | Postgres connection string. When set, the ingestor dual-writes JSONL + Postgres |
| `POLYMARKET_DATA_DIR` | Polymarket ingestor + API | Override JSONL directory (defaults to `services/ingestor/data/`) |
| `POLYMARKET_WATCHLIST_PATH` | Polymarket ingestor | Wallet watchlist file (auto-seeded each pipeline run) |
| `INGEST_POLYMARKET` | Polymarket ingestor | Kill switch for the legacy `all` CLI subcommand. The web button bypasses this |
| `SIGNAL_WEBHOOK_URL` | API | When set, new skilled-bet signals and exit signals are POSTed here as JSON after each ingest (at-least-once; first pass after deploy never floods backlog) |
| `INGEST_EVERY_MINUTES` | API | When >0, an in-process scheduler dispatches the same pipeline run as the "Run ingest" button on this interval (first run ~60s after boot; busy ticks skip) |
| `INGEST_DEEP_EVERY_N_RUNS` | API | With the scheduler on, every Nth scheduled run is a **deep** discovery pass instead of a shallow refresh (0/unset = never deep) |
| `INGEST_DEEP_WALLET_BATCH_SIZE` | API deep run | Positive wallet batch size; default 25 |
| `INGEST_DEEP_LEADERBOARD_DEPTH` | API deep run | Positive leaderboard depth; default 100 |
| `INGEST_RECENT_TRADER_LIMIT` | API deep run | Positive discovery wallet limit; default 1000 |
| `INGEST_RECENT_TRADER_MAX_PAGES` | API deep run | Positive discovery page limit; default 20 |
| `DATA_STALE_AFTER_MINUTES` | API | Freshness limit exposed by `/platform/status`; see deployment defaults |
| `ADMIN_API_TOKEN` | API | Bearer credential required for operator routes, including metrics; never expose to public frontend |
| `ALLOW_UNAUTHENTICATED_ADMIN` | API development | Explicit local-only operator bypass; forbidden in cloud mode |
| `VITE_SHOW_INGEST_CONTROLS` | Dashboard development | Shows ingest controls in the dev server when `1`; production builds never show them |
| `FASTLANE_EVERY_SECONDS` | API | When >0, an in-process fast-lane poller fetches ONLY the activity feed for the top tailable wallets on this interval and webhook-delivers new BUY/SELL trades immediately (clamped to ≥30s; alert-only — never writes the JSONL stores) |
| `FASTLANE_WALLETS` | API | Wallets the fast lane polls per tick, ranked by `rank_score` (default 25, capped at 100) |
| `FASTLANE_MIN_ENTRY_USDC` | API | Fast-lane alerts ignore trades below this USDC size (default 0 = all) |

---

## Running things locally

```sh
# API (from the repo root)
ALLOW_UNAUTHENTICATED_ADMIN=1 uv run --no-sync uvicorn marketsignalos_api.main:app --reload --port 8000

# Dashboard dev server, pointed at the local API
VITE_API_BASE_URL=http://localhost:8000 pnpm --filter @marketsignalos/dashboard run dev

# Polymarket pipeline CLI: the same orchestrator the "Run ingest" button uses.
# Subcommands: pipeline (recommended), seed-watchlist, wallets, markets,
# enrichment, fetch-kalshi-markets, match-markets, review-matches, all, leaderboard.
uv run --no-sync marketsignalos-polymarket-ingestor pipeline

# Lean-pilot worker, read-only plan (no network, writes nothing)
uv run --no-sync python -m marketsignalos_polymarket.lean_pilot --data-dir /tmp/pilot --config deploy/lean-pilot.json --plan
```

---

## Testing, linting and type checking

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check apps/api services/polymarket-ingestor scripts
uv run --no-sync mypy apps/api services/polymarket-ingestor scripts --config-file pyproject.toml

pnpm --filter @marketsignalos/dashboard run typecheck
pnpm --filter @marketsignalos/dashboard run test
pnpm --filter @marketsignalos/dashboard run build
```

CI (`.github/workflows/ci.yml`) runs the Python checks on Linux and Windows and the
dashboard checks on Linux for every PR; all must pass before merging. The worker
image and the Pages build have their own workflows.

- Ruff: 100-char lines, Python 3.13 target
- MyPy: strict mode; all public functions need type annotations. A few older
  `scripts/` files are excluded in `pyproject.toml` until they are typed.

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
                  React/Vite dashboard (apps/dashboard): `/`
```

The pipeline runs JSONL-first. Postgres is opt-in via `DATABASE_URL` (`Dual*` stores fan every write out to JSONL **and** Postgres). API reads always come from JSONL.

### Key API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Minimal HTML landing page (orientation only; the dashboard is apps/dashboard) |
| `GET` | `/health` | Health check (used by Railway) |
| `GET` | `/metrics` | Prometheus text exposition — request RED, pipeline stages, upstream client, model health, feed cache (see `docs/observability.md`) |
| `POST` | `/ingestor/run` | Triggers one pass of `run_pipeline()`; returns 202 + started_at |
| `POST` | `/ingestor/run/deep` | Triggers one deep discovery pass (categorized leaderboard sweep + recent-trader discovery); shares the single running flag with `/ingestor/run` |
| `GET` | `/ingestor/status` | Live state: running flag, last_exit_code, last_error, log_tail, last_summary, schedule (background-scheduler state; null when off) |
| `GET` | `/ingestor/watchlist` | Current wallet watchlist (manual + auto-seeded, merged) |
| `POST` | `/ingestor/watchlist` | Manually add a wallet (`{"address": "0x…"}`) — validated, deduped, pinned so archival can never drop it; hydrated on the next pipeline run. 409 while a run is in flight |
| `GET` | `/signals/skilled-bets` | Still-held BUY entries by skilled wallets, joined with the Kalshi mirror (headline endpoint — drives the `/` dashboard). Fresh entries rank above priced-in ("late") ones |
| `GET` | `/signals/polymarket-leaderboard` | Skilled wallets ranked by on-chain win rate; `archetype=systematic` filters to automation-shaped wallets |
| `GET` | `/signals/ledger` | Paper-trade ledger: every surfaced signal with its surface-time prices and settlement status |
| `GET` | `/signals/ledger/summary` | Hit rate + hypothetical $1-per-signal tail ROI (at surface price and at wallet entry) |
| `POST` | `/signals/ledger/refresh` | Record newly surfaced signals + settle resolved ones (runs automatically after API-triggered ingests) |
| `GET` | `/signals/market-consensus` | Markets grouped by skilled-wallet confluence: wallets per side, capital-weighted entries, contested flag |
| `GET` | `/signals/exits` | Exit signals: skilled wallets that closed/trimmed ≥50% of a still-open position (un-tail alerts) |
| `POST` | `/signals/exits/refresh` | Diff latest position snapshots and record new exits (runs automatically after API-triggered ingests) |
| `GET` | `/signals/notifications/status` | Webhook configuration + delivery watermarks |
| `POST` | `/signals/notifications/run` | Deliver new signals/exits to the `SIGNAL_WEBHOOK_URL` webhook (runs automatically after API-triggered ingests) |
| `GET` | `/signals/fastlane/status` | Fast-lane poller state: config, tracked-wallet watermark count, last tick outcome |
| `POST` | `/signals/fastlane/run` | Run one fast-lane polling pass immediately (manual check or external-scheduler mode) |
| `GET` | `/signals/wallets/{proxy_wallet}` | Wallet dossier: enrichment scores, open positions, bet history with CLV, equity curve, category breakdown, ledger + exit rows (drives the `/wallet/[address]` page) |
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
| `consensus_wallets` (per bet) | Distinct skilled ENTITIES holding the same (condition, outcome) side — wallets sharing a display name cluster as one entity (cheap dedupe heuristic; `consensus_accounts` keeps the raw address count); `consensus_contested` flags markets where skilled wallets hold BOTH sides |
| exit signals | Snapshot-diff detection of skilled wallets closing (`closed`) or trimming ≥50% (`trimmed`) a position while the market is still active in Gamma — redemptions on resolved markets are never exits |
| `clv_*` (per wallet) | Gate 13's CLV, event-capped and capital-weighted. `forecast-v5` (the lean pilot since 2026-10-05, `post_entry_clv.py`): each BUY fill at least seven days before its market's scheduled end, against the price 1 h later minus the price paid, any bet status. `forecast-v4` (code default): closing/current line − buys-only entry VWAP, a reference that sits at the outcome for most resolved bets (blueprint decision 6) |
| `category_edges` (per wallet) | Per-category partial-pooling refit of the Bayesian edge model (wallet's lifetime posterior as prior). Feed rows carry `category_skill_*` for the entry's own category; a proven category (≥5 independent events) caps the tail-EV edge, a thin one falls back to the wallet-level score |
| `price_lead_score` (per wallet) | Does the market tend to follow this wallet's entries? Mean of three explainable sub-scores: post-entry drift within 48h (from price snapshots), longshot conversion (win rate on ≤25¢ entries vs implied), late-entry accuracy (entries within 72h of close vs implied). Descriptive only — never gates tailability; boosts fast-lane watchlist ranking |
| `conviction` (per bet) | Entry USDC z-scored against the wallet's own BUY-size history (≥5 buys required): `high` (z≥2) boosts feed rank within the same remaining-edge tier; `entry_pct_of_bankroll` divides by the latest wallet portfolio value |
| `style_archetype` / `automation_score` (per wallet) | Deterministic trading-style classification — `systematic` / `mixed` / `discretionary` / `unclassified` — averaged from five explainable sub-signals (trades/active day, median inter-trade gap, 24h UTC coverage, repeated-size buys, markets/active day), each contributing a driver string. Descriptive only; never gates tailability |
| `kalshi_match_confidence` | TF-IDF cosine similarity between Polymarket and Kalshi market titles (±3-day date window) |

---

## What is implemented vs. planned

### Implemented
- FastAPI app: `/` (minimal HTML landing), `/health`, `/metrics`, `/ingestor/{run,status}`, `/signals/skilled-bets`, `/signals/polymarket-leaderboard`
- **`run_pipeline()`** — single in-process orchestrator the web "Run ingest" button invokes. Seeds wallets from the data API's `/v1/leaderboard` across `day/week/month/all` windows (a rejected window is skipped and named in the result's `warning`), pulls activity/positions/value, fetches Polymarket markets + Kalshi public markets, and runs the Polymarket→Kalshi market matcher. No env vars required.
- **`/signals/skilled-bets`** — still-held BUY entries from wallets with `skill_likelihood ≥ 0.8`, each row carrying the Kalshi mirror (ticker, title, deep link, live YES price, match confidence) when a match exists
- **SkilledBetsPanel + PolymarketLeaderboardPanel + IngestButton** mounted on `/` (the dashboard root)
- **Operator ingestion** — bearer-token protected HTTP controls; explicit local-only bypass and development buttons. Log capture surfaces a `log_tail` and counts summary to authorized operators.
- **Polymarket Postgres write path** — Alembic schema (`services/polymarket-ingestor/alembic/`) covers 8 tables; `Dual*` store wrappers fan every write out to JSONL and Postgres when `DATABASE_URL` is set; API reads remain JSONL-only
- **Kalshi parlay-ticker filter** — `_is_kalshi_parlay()` excludes `KXMVE*` multi-leg tickers from the matcher
- **Recency-weighted edge (`forecast-v3`)** — a second Bayesian fit with each bet's likelihood weight decayed at a 180-day half-life; `recent_*` enrichment fields plus two new tailability gates (recent independent events ≥ 5; recent edge not negative)
- **Remaining-edge gate** — each feed row carries `move_captured_pct` + `remaining_edge_status`; the feed sorts fresh/discounted entries above partial ones with late last, and `max_move_captured` can drop converged signals entirely
- **Signal outcome ledger** — `signal_ledger.jsonl` records every surfaced signal once (at its surface-time prices) and settles it when the market resolves; `/signals/ledger*` endpoints expose rows, hit rate, and hypothetical tail ROI
- **Market consensus** — feed rows carry `consensus_wallets`/`consensus_accounts`/`consensus_contested`; `/signals/market-consensus` groups markets by skilled-wallet confluence with capital-weighted average entries. Consensus counts are entity-deduped: wallets sharing a display name cluster as one entity (labeled heuristic; on-chain funding-source clustering is the rigorous upgrade)
- **Manual watchlist** — `POST /ingestor/watchlist` adds a wallet by address (validated, deduped, review-state pinned so dormancy archival never drops it); a "Watch wallet" input in the dashboard nav drives it, and the next pipeline run hydrates + scores the wallet
- **Exit signals** — `exit_signals.jsonl` + `exit_state.json` watermark; diffs each skilled wallet's latest two complete position snapshots, suppressing resolved-market redemptions; surfaced via `/signals/exits` and an ExitSignalsPanel on the dashboard
- **Webhook notifier** — `SIGNAL_WEBHOOK_URL` receives one JSON POST per ingest with new signals + exits; watermark state in `notifications_state.json`, at-least-once delivery, failed posts retry the same batch next pass
- **CLV (closing-line value)** — `polymarket_price_snapshots.jsonl` appends one price point per market per fetch (the deduplicating markets store discards history); enrichment carries `clv_mean`/`clv_lower_bound`/`clv_sample_size`; Alembic migration `0005`
- **Wallet bet records** — `polymarket_wallet_bets.jsonl` rewritten each enrichment pass with one row per (wallet, condition, outcome): status (`won`/`lost`/`exited`/`open`), buys-only entry VWAP, PnL, per-bet CLV
- **Conviction score** — per-bet sizing z-score computed in the same single activity-file stream the feed already does; high-conviction entries badge ("Sized up") and rank above same-tier rows
- **Wallet detail page** — `/wallet/:address` (dashboard) renders the dossier from `/signals/wallets/{wallet}`: stat strip, SVG equity curve, bet history with CLV, category breakdown, open positions, recent exits; dashboard wallet names link to it
- **Trader-style (archetype) scoring** — `trader_style.py` classifies each wallet's operational footprint (`systematic`/`mixed`/`discretionary`/`unclassified`) from five explainable sub-signals computed in the enrichment pass; surfaced on the leaderboard (`archetype` filter + chips), feed rows (`wallet_archetype`, "Systematic" badge), and the wallet page (Trading style card with drivers + raw metrics). Alembic migration `0006`
- **Scheduled ingest** — `INGEST_EVERY_MINUTES` starts an in-process asyncio scheduler (FastAPI lifespan) dispatching the exact run path the dashboard buttons use (same running flag, log capture, and post-ingest hooks); `INGEST_DEEP_EVERY_N_RUNS` makes every Nth run a deep discovery sweep; schedule state surfaces in `/ingestor/status`
- **Tail-EV score** — every feed row carries `tail_fair_price` (the wallet's conservative log-odds edge bound applied to its entry-implied probability) and `tail_ev` (fair minus the EXECUTABLE price, $/share — the picked side's ask when the Gamma book is known (`tail_ev_source="ask"`), else the mark); rows rank by tail EV within each remaining-edge tier, `min_tail_ev` filters the feed, and `/signals/ledger/summary` adds `calibration` (realized win rate vs market-implied probability at surface time), `open_mark_to_market`, and per-dimension `breakdowns` (including `book_status`)
- **Order-book spread awareness** — feed rows carry `best_bid`/`best_ask` (YES-side book top), `spread_cents`, `book_status` (`tight` ≤3¢ / `wide` / `unknown`), and `tail_ask_price` (NO-side tails execute at 1 − bid); the dashboard shows a "Wide book" badge on >3¢ spreads and the ledger freezes book context on every row so the summary can compare tight- vs wide-book performance
- **Per-category skill (`forecast-v4`)** — enrichment rows carry `category_edges` (partial-pooling refit per Gamma category with the wallet's lifetime posterior as prior; Alembic migration `0007`); feed rows carry `category_skill_likelihood`/`category_edge_lower_bound`/`category_independent_events`/`category_skill_source` for the entry's own category, a proven category fit caps `tail_edge_used`, and the dashboard shows a "{category} fit N%" chip when the category fit governs
- **Price-lead (informed-flow footprint) scoring** — `price_lead.py` scores whether the market tends to follow each wallet's entries from three explainable sub-signals (post-entry drift within 48h via price snapshots, longshot conversion vs implied, late-entry accuracy vs implied), event-capped like the skill fit; enrichment carries `price_lead_score` + `price_lead_drivers` + raw metrics (Alembic migration `0008`), feed rows carry `wallet_price_lead_score` (dashboard "Price lead" badge at ≥0.6), and the fast-lane watchlist ranks by `rank_score × (1 + price_lead_score)`. Descriptive and non-accusatory: it labels how the market reacts, never claims why
- **Fast-lane poller** — `FASTLANE_EVERY_SECONDS` starts a second in-process loop that polls ONLY `/activity` for the top-N tailable wallets (by `rank_score`, boosted by `price_lead_score`) and webhook-delivers new BUY/SELL trades within one interval. Alert-only by design: it never writes the JSONL stores (the pipeline stays the sole writer and re-fetches the same events with dedupe); watermarks live in `fastlane_state.json`, first sight of a wallet initializes silently, and failed webhook POSTs retry the same batch (at-least-once)

- **Observability** — `/metrics` exports real collectors instead of default process stats: request RED keyed on the route template, per-stage pipeline durations + RSS, a progress heartbeat that makes a wedged run detectable without an error, upstream request timing split into *construction* vs transport, and model-health gauges (`resolved_bets`, population prior mu/sigma2, skill saturation ratio). `ops/prometheus/alerts.yml` carries 13 rules: seven are regression guards derived one-for-one from the post-mortems in `docs/fix-lessons-learned.md` (each tagged with its incident date), six are SLOs. `apps/api/tests/test_alert_rules.py` fails CI if a rule references a metric nothing exports, so renaming a collector can't silently disarm an alert. Full inventory and per-alert runbooks in `docs/observability.md`
- **Grafana Cloud delivery** — `ops/grafana/` carries the Alloy scrape config, a Dockerfile, a 20-panel dashboard, and a setup runbook. Grafana Cloud's hosted Prometheus is push-based, so Alloy runs as a second small service that scrapes the API's `/metrics` and `remote_write`s it (Grafana Agent is EOL as of 2025-11-01 and its config format does not carry over). `apps/api/tests/test_grafana_dashboard.py` fails CI if a panel queries a metric nothing exports, if a panel has no description, if panels overlap, or if a metric that can fire an alert has no panel to look at

### Pending / in progress
- `/positions` pagination (currently caps at ~100 per wallet)
- Embedding-based matcher (only if TF-IDF precision proves inadequate)
- Full OpenTelemetry trace propagation (stage durations exist; no span causality yet)
- Runbook (`docs/runbook.md`) — per-alert runbooks now live in `docs/observability.md`
- **Never unpin ruff/mypy** in either package's `dev` extras. They are gate-keeping CI linters that expand what they check between releases: ruff `0.16.0` (2026-07-23) widened its default rule set and turned a green `main` red with no code change. Bump them deliberately, with the resulting fixes in the same PR. `TRY004` is ignored by policy in both packages — see the rationale in `services/polymarket-ingestor/pyproject.toml`

---

## Deployment

Keep the $15/month target and the prepare-before-provisioning rule.

- **Website:** `.github/workflows/pages.yml` builds `apps/dashboard` with
  `VITE_STATIC_SITE=1` and publishes it to GitHub Pages. It reads the research
  snapshot that `.github/workflows/research-snapshot.yml` publishes to the
  `codex/research-snapshots` data branch every six hours. No API is involved.
- **Worker:** Railway reads `railway.toml`, builds `deploy/worker.Dockerfile`, and
  runs the lean pilot hourly as a cron service with a volume. See
  `docs/railway-deployment.md` and `docs/lean-pilot.md`.
- **API:** not deployed. `scripts/start-api.sh` starts it with the same
  `PYTHONPATH` the tests use; serving it publicly is blueprint Stage 5.

---

## Conventions

- **One router per route file** under `apps/api/src/marketsignalos_api/api/routes/`
- **Services live in** `apps/api/src/marketsignalos_api/services/` — no business logic in route files
- **Pydantic models** for all request/response schemas
- **No `print()`** — use the stdlib `logging` module with structured output
- **Dashboard** is a client-rendered React app; API calls go through `src/lib/api-base.ts`, and the static build makes none

---

## Docs

| File | Contents |
|---|---|
| `docs/handoff-blueprint.md` | **Authoritative build reference.** Corrected architecture, staged plan with entry gates and acceptance evidence, invariants, data contracts, kill criteria |
| `docs/gate-attrition.md` | Stage 0 command, reproducibility, verification scope, and next steps |
| `docs/metadata-coverage.md` | Stage 1 stale-coverage repair, frozen audit, remaining coverage semantics, and reproduction |
| `docs/benchmarks/2026-09-10-metadata-coverage.md` | 833-wallet coverage decomposition and same-input gate replay; JSON/notebook companions |
| `docs/benchmarks/2026-09-10-gate-attrition.md` | Frozen 833-wallet waterfall, overlap, counterfactuals, and scoped conclusion; JSON/notebook companions |
| `docs/benchmarks/2026-09-28-prior-floor.md` | Stage 0 follow-up: prior variance floor, any-prior qualifier bound, estimator refit (`prior_floor` command) |
| `docs/railway-deployment.md` | Railway worker service: config, provisioning steps, what is unverified |
| `docs/research-snapshot.md` | Scheduled public research capture and its publication branch |
| `docs/platform-roadmap.md` | Backend/platform milestones and implementation status |
| `docs/research-credibility.md` | Statistical evidence, point-in-time evaluation, and public-claim standards |
| `docs/lean-pilot.md` | $15-target worker limits, recovery, and publication constraints |
| `docs/storage-benchmark.md` | Offline activity JSONL/Parquet benchmark and source integrity |
| `docs/enrichment-shadow.md` | Full scorer parity and memory measurements |
| `docs/benchmarks/2026-09-29-price-history-probe.md` | Live probe: closing prices recoverable at 1 h for markets closed 2023+ via explicit windows; basis for `closing_lines.py` |
| `docs/benchmarks/2026-10-05-gate13-power.md` | First gate-13 power report: v4 vs v5 counts, per-wallet 1 h CLV power, exclusions; D4 evidence |
| `docs/gate13-clv-v5-plan.md` | Plan for gate 13 on 1 h post-entry CLV (`forecast-v5`): definition, owner decisions D1–D5 (chosen 2026-10-05), build steps |
| `docs/stage3-cohort-v1-plan.md` | Stage 3 cohort v1: the rule qualifies no wallet, so nested tiers with T2 primary; outcome, costs, baseline, window and timing decisions S1–S7 (chosen 2026-10-06); build steps |
| `docs/llm-judge.md` | Market-matcher eval set, TF-IDF baseline tooling, and the planned LLM judge (Phase 1 built) |
| `docs/benchmarks/2026-09-29-polygon-logs-probe.md` | Live probe: V2 `OrderFilled` decodes to Data API trades exactly; ~2.9 GB/day to follow the chain, ~970 calls per wallet history on free RPC; Goldsky orderbook subgraph is deprecated |
| `docs/prd.md` | Product requirements, MVP scope, success metrics |
| `docs/architecture.md` | High-level data flow and key principles |
| `docs/0001-tech-stack.md` | ADR explaining stack choices |
| `docs/0002-cross-exchange-decision.md` | ADR for the Polymarket pivot (with the 2026-05-23 correction reversing the cross-exchange framing) |
| `docs/polymarket-pipeline.md` | Full Polymarket data-flow reference |
| `docs/polymarket-api-discovery.md` | Validated public Polymarket endpoint shapes |
| `docs/phase4-roadmap.md` | Prioritized Phase 4+ roadmap (bot discovery + insight-trader tailing) |
| `docs/observability.md` | Metric inventory, alert reference, incident→alert coverage |
| `docs/runbook.md` | Operational runbook (WIP) |
| `docs/fix-lessons-learned.md` | Post-mortems and lessons |
