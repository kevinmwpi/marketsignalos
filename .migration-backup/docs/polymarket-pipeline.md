# Polymarket Pipeline

End-to-end reference for the skilled-bets feed. Built up across Phases 0-6
(see `polymarket-phase-status.md`). A 2026-05-23 correction reversed the
short-lived "cross-exchange dislocation" framing: Kalshi is the tail target
where the operator places the bet, not a price-comparison side. The matcher,
`market_links.jsonl`, and Kalshi market fetch all remain — they back the
per-row Kalshi mirror on the skilled-bets feed.

---

## Why

Kalshi hides individual user bet history on social profiles, so even with
a leaderboard scrape there's no way to compute a true on-chain win-rate.
Polymarket settles every trade on Polygon and exposes wallet history
publicly. We use Polymarket as the source-of-truth for skill, then look up
the equivalent Kalshi market for each skilled wallet's currently-held bet
via a title-similarity matcher, so US-based operators can tail the trade
on Kalshi.

See `0002-cross-exchange-decision.md` for the strategic decision and the
2026-05-23 correction.

---

## Data flow

```
Polymarket leaderboard API
        ↓
  Top wallets by profit / volume
        ↓
Polymarket Data API (/activity, /positions, /value per wallet)
        ↓
polymarket_activity.jsonl  +  polymarket_positions.jsonl
        ↓
Gamma /markets  ──→  polymarket_markets.jsonl
        ↓
skill_computation.py  →  polymarket_wallet_enrichment.jsonl
                          (resolved_trades, wins, losses,
                           skill_likelihood, total_pnl_usdc)

Kalshi public /markets  ──→  kalshi_markets.jsonl
                          (ticker, title, yes_bid, yes_ask)

market_matcher.py (TF-IDF + date bucketing)
  takes:    polymarket_markets.jsonl × kalshi_markets.jsonl
  produces: market_links.jsonl  (auto-approved / pending / rejected)

skilled_bets.py service joins:
  - polymarket_wallet_enrichment.jsonl  (filter by min_skill)
  - polymarket_positions.jsonl          (most-recent snapshot per leg; still-held only)
  - polymarket_activity.jsonl           (latest BUY entry per position)
  - polymarket_markets.jsonl            (current YES price + category)
  - market_links.jsonl                  (best non-rejected Kalshi mirror per condition_id)
  - kalshi_markets.jsonl                (current YES price for the mirror)

GET /signals/skilled-bets → still-held skilled bets, each with a Kalshi mirror
GET /signals/polymarket-leaderboard → ranked skilled wallets
```

---

## File locations

All Polymarket JSONL files live in `services/ingestor/data/` by default
(same dir as Kalshi files). Override with `POLYMARKET_DATA_DIR`.

| File | Producer | Consumer |
|---|---|---|
| `polymarket_leaderboard.jsonl` | `seed-watchlist` mode | `seed-watchlist`, enrichment (display names) |
| `polymarket_wallet_watchlist.txt` | `seed-watchlist` | `wallets` mode |
| `polymarket_activity.jsonl` | `wallets` mode (dedup by tx+cond+outcome+type) | `enrichment` mode |
| `polymarket_positions.jsonl` | `wallets` mode (appended snapshots) | `skilled_bets.py` service |
| `polymarket_wallet_values.jsonl` | `wallets` mode | not yet consumed |
| `polymarket_wallet_checkpoints.json` | `wallets` mode | `wallets` mode (steady-state) |
| `polymarket_markets.jsonl` | `markets` mode (dedup by cond+closed status) | matcher, `skilled_bets.py` |
| `kalshi_markets.jsonl` | `fetch-kalshi-markets` mode (overwrite) | matcher, `skilled_bets.py` |
| `polymarket_wallet_enrichment.jsonl` | `enrichment` mode (overwrite) | `polymarket-leaderboard` API, `skilled_bets.py` |
| `market_links.jsonl` | `match-markets` mode (upsert; manual overrides sticky) | `skilled_bets.py` |

---

## CLI subcommands

The `marketsignalos-polymarket-ingestor` entry point (from `services/polymarket-ingestor`):

| Subcommand | What it does |
|---|---|
| `seed-watchlist` | Pulls top-N from `/profit` + `/volume` leaderboards, unions with existing watchlist (preserving manual additions), writes back. |
| `leaderboard` | Standalone leaderboard fetch with arbitrary metric/window — useful for historical series. |
| `wallets` | For each address in the watchlist, paginates `/activity` until the per-wallet checkpoint, refreshes positions + value. Supports `--full-backfill` to ignore checkpoints. |
| `markets` | Pages Gamma `/markets`; supports `--closed`, `--active`, `--order`, `--ascending`, and `--backfill-from-activity` (the last fetches any condition_ids present in activity but missing from the markets cache). |
| `enrichment` | Reads activity + markets, runs skill computation, overwrites `polymarket_wallet_enrichment.jsonl`. |
| `fetch-kalshi-markets` | Pages Kalshi `/markets` (public, no auth) into `kalshi_markets.jsonl`. |
| `match-markets` | Runs TF-IDF + date matcher across the two market files; upserts `market_links.jsonl` with auto-decisions. |
| `review-matches` | Interactive y/n/s/q prompt for pending matches. Decisions become `matched_by="manual"` and become sticky against future auto re-runs. |
| `all` | Seed → wallets (checkpointed) → markets (closed, desc by endDate) → markets backfill from activity → enrichment. Gated by `INGEST_POLYMARKET=1`. |

---

## Steady-state run order

To rebuild the skilled-bets feed from scratch:

```powershell
# One-time
$env:INGEST_POLYMARKET = "1"
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe seed-watchlist --top-n-profit 50 --top-n-volume 50

# Recurring (every N hours)
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe wallets
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe markets --active --order volumeNum --pages 10
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe markets --backfill-from-activity --pages 0
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe enrichment

# Less frequent (daily)
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe fetch-kalshi-markets --status open
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe match-markets
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe review-matches  # human in the loop
```

Or run the whole thing in one pass:

```powershell
$env:INGEST_POLYMARKET = "1"
.\.venv\Scripts\marketsignalos-polymarket-ingestor.exe all
```

---

## Environment variables

| Variable | Where | Notes |
|---|---|---|
| `POLYMARKET_DATA_DIR` | ingestor + API | Override JSONL directory. Defaults to `services/ingestor/data/`. |
| `POLYMARKET_WATCHLIST_PATH` | ingestor | Override watchlist file. Default: `<data_dir>/polymarket_wallet_watchlist.txt`. |
| `POLYMARKET_ENRICHMENT_PATH` | API | Override path the `polymarket-leaderboard` route reads from. |
| `POLYMARKET_TIMEOUT_SECONDS` | ingestor | HTTP timeout (default 15). |
| `POLYMARKET_MAX_RETRIES` | ingestor | Retries on 429/5xx (default 3). |
| `INGEST_POLYMARKET` | ingestor | Must be `1` / `true` / `yes` / `on` for `all` mode to run. Individual subcommands always run. |
| `KALSHI_BASE_URL` | Kalshi markets fetcher | Defaults to `https://api.elections.kalshi.com/trade-api/v2`. |

---

## Signal semantics

### Skill score (`skill_likelihood`)

Computed in `skill_computation.py` + `bayesian_skill.py`:

1. Aggregate `TRADE` events into per-(condition, outcome) positions; resolve wins from Gamma `outcome_prices >= 0.99`.
2. Fit a hierarchical Bayesian logistic edge model: each bet is judged against **market-implied entry price**, not 50%.
3. `skill_likelihood = P(edge > 0 | data)` with Empirical-Bayes shrinkage across wallets.
4. `independent_settled_events` (ESS) down-weights correlated bets within the same Polymarket event; wallets need ESS ≥ 20 for `tailability_status=tailable`.

`REDEEM` / `MERGE` events are skipped during position aggregation (they have `outcome_index=999`, a Polymarket sentinel for "no direction").

Gamma's `closed` flag is **unreliable** for resolution — use `outcome_prices`. For **tradability**, Gamma `active`/`closed` **is** used to gate the default skilled-bets feed.

### Skilled-bets feed

Computed in `skilled_bets.py`. Each row carries:

- Latest BUY entry + signal age (`max_bet_age_days` filter supported).
- `tradability` + reasons (`poly_direct`, `kalshi_mirror`, `on_chain_only`, `closed`).
- Polymarket deep link (preferred CTA when tradable).
- Approved Kalshi mirror only (`kalshi_match_status=approved`); pending matches never get a tail URL.
- `kalshi_vs_entry_cents` — Kalshi YES vs wallet entry (not vs current Poly).

Default API behavior hides `on_chain_only` and `closed` rows (`include_untradable=false`).

### Match confidence

TF-IDF cosine on tokenized titles after a `(category_bucket, end_date ±N days)` pre-filter. Tunable thresholds in `MatchConfig`:

- `>= 0.75`: `auto-approved`
- `0.35..0.75`: `pending` (review queue)
- `< 0.35`: dropped

Manual decisions (`matched_by="manual"`) override auto decisions on subsequent matcher runs — that's how repeat false positives get permanently silenced.

---

## Known limitations

1. ~~**Kalshi parlay tickers have concatenated titles.**~~ Resolved: `_is_kalshi_parlay()` in `runner.py` filters `KXMVE*` tickers out of the matcher's input. Raw `kalshi_markets.jsonl` is preserved (still useful for orderflow); the prefix list is a one-line constant if more parlay families need to be added.
2. **`/positions` pagination.** The ingestor paginates `/positions` via `_paginate_positions()`; very large books may still hit the safety bound — monitor hydration errors.
3. **Activity sample depth.** Default `max-pages-per-wallet=40` (~20k events). Historical depth beyond that requires `--full-backfill`.
4. **Postgres backend not wired.** All stores fall through to JSONL. Postgres stubs exist in `storage.py` for the eventual migration. Tracked as Phase 7.5.
5. **TF-IDF over neural embeddings.** Picked for zero-dependency simplicity. Recall on long paraphrases ("Will Donald Trump win the 2024 election?" vs "2024 US Presidential winner: Trump?") is below ideal. Tracked for Phase 8 as a swap to `sentence-transformers` if precision/recall numbers demand it.

---

## API surface

| Method | Path | Returns |
|---|---|---|
| GET | `/signals/polymarket-leaderboard?min_resolved=N&min_skill=X&limit=N` | List of skilled wallets, sorted by skill_likelihood |
| GET | `/signals/skilled-bets?...&include_untradable=false` | Actionable skilled bets (default hides closed/on-chain-only) |
| GET | `/signals/skilled-bets/summary` | Wallet trust counts + feed tradability breakdown |

Both routes are read-only and idempotent. The data they serve is refreshed only by ingestor runs.

---

## Dashboard

`apps/web/app/page.tsx` consumes both endpoints via `getDashboardData()`:

- `SkilledBetsPanel` (full-width, headline) — one row per still-held bet from a skilled wallet, with entry price/size/timestamp, current Polymarket YES price + drift vs entry, still-held size, and a "Mirror on Kalshi" call-to-action block (ticker, title, deep link, live YES price, cent delta vs Polymarket) when a non-rejected match exists.
- `PolymarketLeaderboardPanel` (sidebar) — skill %, W/L, color-coded PnL, "active N ago" stamp.

Both panels degrade gracefully to copy-explained empty states when the relevant JSONL files don't exist yet.
