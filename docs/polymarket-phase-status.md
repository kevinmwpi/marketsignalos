# Polymarket Pipeline Phase Status

Tracks completion against the cross-exchange signal plan. For the durable
architecture reference see `polymarket-pipeline.md`; for the strategic
rationale see `0002-cross-exchange-decision.md`.

| Phase | Title | Status | Commit |
|---|---|---|---|
| 0 | Discovery (API probing) | done | `e31a2bf` |
| 1 | Ingestor scaffold | done | `e31a2bf` |
| 2 | Pagination + checkpoints + watchlist seeding | done | `c2fdfc9` |
| 3 | On-chain skill scoring + `/signals/polymarket-leaderboard` | done | `f8a7ab9` |
| 4 | TF-IDF market matcher + sticky manual review | done | `881853c` |
| 5 | Cross-exchange dislocation `/signals/cross-exchange` | done | `01ba343` |
| 6 | Dashboard panels (Cross-Exchange + Polymarket wallets) | done | `b8f448c` |
| 7 | CI + docs + INGEST_POLYMARKET kill switch | in progress | (this commit) |
| 7.5 | Postgres migrations for the new tables | pending | — |
| 8 | Matcher tuning + Kalshi parlay filtering + position pagination | pending | — |

## Phase 0 — Discovery

Validated public Polymarket data surfaces (Gamma, CLOB, Data API,
leaderboard API, Goldsky subgraph). Confirmed every read endpoint we use
is unauthenticated. Captured field shapes in `polymarket-api-discovery.md`.
Reproducible via `scripts/probe_polymarket.py`.

## Phase 1 — Ingestor scaffold

`services/polymarket-ingestor` Python package with sync httpx client,
dataclass models, JSONL stores, and CLI subcommands. Sync was chosen over
async to keep the scaffold simple while endpoint shapes were still being
nailed down.

## Phase 2 — Pagination + checkpoints

- `/activity` paginates via `offset` (newest-first, no overlap between pages).
- `JsonWalletCheckpointStore` records the highest-timestamp event seen per wallet.
- `seed-watchlist` unions top-N profit + volume into the watchlist file, preserving manual additions.
- Live two-pass validation: run 1 ingested 900 events across 9 wallets; run 2 fetched 0 new for 8/9 wallets and exactly 1 for the one wallet that traded between runs.

## Phase 3 — Skill scoring

`skill_computation.py` joins activity to resolved markets:

- Aggregate TRADE events into `(condition_id, outcome_index)` positions with net size, total cost, and realized PnL from exits.
- A market is "resolved" if `outcome_prices` shows a >= 0.99 winner. Gamma's `closed` flag is unreliable (markets with future end dates can be flagged closed).
- `skill_likelihood = Phi((wins − n*0.5) / sqrt(n*0.25))` — normal approximation of the binomial test.
- Backfill: any condition_id present in activity but absent from the markets cache is fetched via `gamma/markets?condition_ids=`.

API surface: `/signals/polymarket-leaderboard?min_resolved=N&min_skill=X&limit=N`.

## Phase 4 — Market matcher

Pure-Python TF-IDF + cosine similarity over short titles, no scikit-learn dependency.

- Tokenize with trailing-`s` lemmatization ("50bps" → "50bp", "rates" → "rate").
- Pre-filter by `(category_bucket, end_date ±N days)`. On 2000 Kalshi × 999 Polymarket markets this prunes 1.998M brute pairs to ~280k candidates.
- Thresholds: ≥0.75 auto-approves, 0.35..0.75 goes to the `pending` queue, <0.35 dropped.
- `JsonlMarketLinkStore.upsert_links()` makes manual decisions sticky — `matched_by="manual"` rows cannot be overwritten by auto re-runs. This is how repeat false positives stay silenced.
- `review-matches` CLI walks the pending queue with y/n/s/q prompts.

Live validation found 4 real cross-exchange pairs (Lorenzo Musetti French Open on both exchanges, IPL cricket match), all correctly routed to manual review.

## Phase 5 — Cross-exchange signal

`apps/api/src/marketsignalos_api/services/cross_exchange.py` joins five sources:

1. enrichment (filter by `min_skill`)
2. positions (collapse to latest snapshot per leg)
3. market_links (skip `rejected`)
4. polymarket_markets (current YES price)
5. kalshi_markets (current YES price, cents → decimal)

Outputs ranked `CrossExchangeSignal` rows. NO positions flip both sides
to the YES complement so prices are quoted on the same scale. The
`recommended_action` string spells out which exchange to buy on.

API surface: `/signals/cross-exchange?...`. 10 integration tests including the manual-rejection stickiness propagating to the API.

## Phase 6 — Dashboard

`CrossExchangePanel` is the new marquee section. Side-by-side price
comparison with directional arrow, emerald chip on the cheaper exchange,
wallet provenance footer (skill %, resolved trades, USD held).
`PolymarketLeaderboardPanel` is the new sidebar panel (W/L, color-coded
PnL, "active N ago" freshness). Both gracefully render empty states.

## Phase 7 — Operationalization (in progress)

- CI: new `polymarket` job in `.github/workflows/ci.yml` (ruff → mypy → pytest).
- `INGEST_POLYMARKET` env flag: the `all` mode no-ops unless set to `1`/`true`/`yes`/`on`. Lets the binary deploy alongside the API without triggering load.
- Docs: this file, `polymarket-pipeline.md` (full data flow reference), `0002-cross-exchange-decision.md` (ADR).
- CLAUDE.md updated to note the pipeline.

## Phase 7.5 — Postgres (pending)

JSONL stores work today. Postgres stubs exist in `storage.py`. New
Alembic migrations needed for: `polymarket_activity`,
`polymarket_positions`, `polymarket_markets`, `polymarket_wallet_enrichment`,
`market_links`. Mirror the kalshi-ingestor migration pattern.

## Phase 8 — Tuning (pending)

- ~~**Kalshi parlay filtering.**~~ Done: `_is_kalshi_parlay()` in `runner.py` excludes `KXMVE*` tickers at the matcher's adapter layer (raw JSONL is preserved).
- **`/positions` pagination.** Endpoint appears to cap at ~100 rows. Either confirm a cursor or fetch positions one condition_id at a time.
- **Embedding-based matcher.** Swap TF-IDF for `sentence-transformers` only if a calibration set of 50 ambiguous pairs shows it materially improves precision/recall.
- **Skill score normalization.** Wallets with very few resolved bets get noisy scores. Add a Beta-Binomial prior or just expose `resolved_trades` prominently (already done in the API + dashboard).

## Verification

```powershell
# Full test suite (91 passing as of Phase 6)
.\.venv\Scripts\python.exe -m pytest services/polymarket-ingestor/tests apps/api/tests -q

# Lint + type-check the new package
.\.venv\Scripts\ruff.exe check services/polymarket-ingestor
.\.venv\Scripts\mypy.exe services/polymarket-ingestor/src

# Web build
cd apps/web; npm run lint; npm run build
```
