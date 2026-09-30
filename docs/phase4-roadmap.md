# Phase 4+ Roadmap — Bot Discovery & Insight-Trader Tailing

*Written 2026-06-11, after Phases 1–3 merged (PRs #24–#26). This document is the
prioritized plan for everything that follows; the P0 slice was implemented in the
same session this doc was written.*

## North star

Surface prediction-market accounts with durable, explainable edge — both
**systematic/algorithmic traders** (the `0xb55f…` reference archetype: high
frequency, consistent sizing, around-the-clock, profitable) and **discretionary
insight traders** (humans who size up where they know something) — fast enough
that a new BUY can be tailed manually while it is still actionable.

Two user stories drive prioritization:

- **A. Bot discovery & tailing** — find systematic wallets that never crack the
  top-50 volume leaderboard, classify their style with explainable drivers,
  and surface their new entries within one ingest cycle.
- **B. Insight-trader tailing** — rank skilled humans higher when they size up
  (conviction, shipped in Phase 3) and stop tailing a politics sharp into a
  sports bet (per-category skill, still open).

A hard constraint shapes everything below: **signals only accrue when ingest
runs**. The ledger, CLV price snapshots, exit diffs, and webhook deliveries are
all per-ingest artifacts. Until ingest is scheduled, every other feature is
throttled by how often someone presses a button.

---

## P0 — implemented in this session

### 1. Scheduled ingest with deep-discovery cadence

| | |
|---|---|
| **Closes** | "Scheduled ingest" gap; operationalizes "Broader discovery" (the deep pipeline existed but only ran when manually clicked) |
| **Impact / effort** | Very high / low — it multiplies the value of everything already built |
| **Data** | None new; reuses the existing run paths including post-ingest hooks (ledger → exits → notifier) |

Design: an `asyncio` background loop inside the API process (started from the
FastAPI lifespan), gated by env vars so default behavior is unchanged:

- `INGEST_EVERY_MINUTES` — when > 0, dispatch a shallow pipeline run on that
  interval (first run ~60 s after boot so crash-loops can't hammer upstream APIs).
- `INGEST_DEEP_EVERY_N_RUNS` — when > 0, every Nth scheduled run is a **deep**
  run (categorized leaderboard matrix + subgraph recent-trader discovery)
  instead of a shallow one. E.g. `INGEST_EVERY_MINUTES=60`,
  `INGEST_DEEP_EVERY_N_RUNS=24` → hourly refresh, one discovery sweep per day.

The scheduler reuses the exact same in-process trigger the dashboard buttons
use (same single running flag, same log capture, same post-ingest hook chain),
so a scheduled run is indistinguishable from a clicked one. Schedule state is
exposed on `GET /ingestor/status` (`schedule` block: enabled, intervals, run
counter, next kind, next ETA). Why in-process rather than Railway cron: the
deployment is a single always-on web process; an in-process loop needs no extra
infra, can't double-run (shares the running flag), and degrades gracefully (a
busy tick just skips).

**Success-criteria link:** webhook + dashboard now see new BUYs within one
*interval* rather than "whenever someone clicks", and bot-rich wallet pools
enter the funnel automatically via the scheduled deep sweeps.

### 2. Trader-style (archetype) scoring

| | |
|---|---|
| **Closes** | "Bot / automation classification" gap |
| **Impact / effort** | High / medium — scoring-only on existing JSONL, no new ingestion |
| **Data** | Computed in the same enrichment pass that already streams each wallet's activity |

Five deterministic sub-signals, each with a raw value, a 0–1 score, and a
human-readable driver string (non-accusatory: "systematic" vs "discretionary",
never "bot detection"):

| Sub-signal | Raw metric | Systematic looks like |
|---|---|---|
| Tempo | trades per active day | hundreds/day (saturates at 50) |
| Cadence | median seconds between consecutive trades | < 60 s (log-scaled down to 0 at ≥ 1 h) |
| Coverage | distinct UTC hours-of-day with ≥ 1 trade | ~24/24 (humans sleep; score 0 at ≤ 12 h) |
| Sizing | share of BUYs at one of the wallet's 3 modal sizes | uniform/repeated sizes |
| Breadth | distinct markets per active day | fans across many markets daily |

`automation_score` = mean of available sub-scores; `style_archetype` =
`systematic` (≥ 0.65) / `mixed` / `discretionary` (≤ 0.35), or `unclassified`
when fewer than 3 sub-signals or < 50 lifetime trades. Style descriptors that
are *not* automation evidence (top category + share, exited-position share,
median exit hold hours) ride along for the wallet dossier.

Surfaces: enrichment JSONL + Postgres (Alembic `0006`), leaderboard endpoint
(+ `archetype` filter param), feed rows (`wallet_archetype`,
`wallet_automation_score`), wallet dossier (drivers + raw metrics), dashboard
badges, wallet-page style card. The archetype never gates tailability — it
describes *how* a wallet trades, not *whether* it has edge.

**Success-criteria link:** the leaderboard can now be filtered to
`archetype=systematic` + positive CLV + recent edge — the reference-bot
screening view — with every label decomposing into drivers.

---

## P1 — next (highest value after the P0 slice)

### 3. Per-category skill decomposition (G4)

- **Why:** story B's biggest open hole — a wallet tailable overall may be
  sharp only in one category; the feed should say "edge *here*, in *this*
  category" and discount out-of-category entries.
- **Sketch:** partial pooling per Gamma category — refit the existing Bayesian
  edge model per (wallet, category) with the wallet's own lifetime posterior as
  the prior (same machinery as the recency fit; `bayesian_skill.py` is reusable
  as-is). Output `category_edges: [{category, skill_likelihood, edge_lower_bound,
  independent_events}]` on enrichment; feed rows gain
  `category_skill_likelihood` for the entry's own category. Categories with
  < 5 independent events fall back to the wallet-level score (with a driver
  string saying so).
- **Effort:** medium. **Files:** `skill_computation.py`, `models.py`, storage +
  migration `0007`, `skilled_bets.py`, panels. **Data:** none new (markets
  already carry `category`). **Tests:** synthetic wallet sharp in category X /
  coin-flip in Y → X edge high, Y shrinks to lifetime prior; feed row carries
  the entry-category score.

### 4. Manual watchlist / seed-by-address

- **Why:** the user already knows wallets worth tracking (e.g. the reference
  bot); today the only way in is to hand-edit the watchlist file on the server.
- **Sketch:** `POST /ingestor/watchlist {address}` validates the 0x address,
  appends to the watchlist file (seeding already preserves manual entries — both
  rewrite sites union with existing), and pins it in deep-review state so it
  can never be archived. Small address-input form next to the ingest buttons.
  Optionally trigger a single-wallet hydration immediately.
- **Effort:** small. **Files:** `routes/ingestor.py`, `runner.py` (pin helper
  exists), `IngestButton.tsx` or a sibling component. **Tests:** append +
  dedupe + invalid-address rejection; pinned state written.

### 5. Positions pagination audit

- **Why (revised):** the handoff lists "~100 per wallet" as a gap, but
  `_paginate_positions` already walks `/positions` at page-size 500 up to a
  100k safety bound — the cap may already be gone. Verify against a high-count
  wallet (the reference bot) and fix only if truncation is real.
- **Effort:** small (verification first). **Tests:** paginator unit test with a
  fake client returning > 500 positions.

---

## P2 — later

### 6. Wallet-cluster dedupe (G9)

One entity running many wallets inflates consensus counts and leaderboard
slots. Deterministic first pass: cluster on shared funding source / first
funder (needs a new on-chain data source — transfer history), or cheaply on
identical name/pseudonym + near-identical entry timing across wallets
(existing data). Consensus counts then dedupe by cluster id. Effort: large for
the on-chain version; do the cheap heuristic first and label it as such.

### 7. PRD "abnormal behavior" signals

Pre-resolution accuracy spike, low-slippage high-accuracy footprint,
cross-market correlated positioning. Each is scoring-only on existing JSONL +
price snapshots (which now accrue on a schedule, making these computable at
all). Build after per-category skill — they share the per-category scaffolding.

### 8. Real-time-ish new-BUY surfacing

With the scheduler shipped, webhook latency ≈ ingest interval, which satisfies
"within one ingest cycle". Going below that means a dedicated lightweight loop
(activity-only poll for watchlist wallets, no full pipeline) — worth it only if
interval-level latency proves too slow in practice. Re-evaluate after running
scheduled for a couple of weeks; the ledger now measures exactly how much ROI
is lost to surfacing delay (`roi_at_surface` vs `roi_at_entry`).

### 9. Embedding-based Kalshi matcher

Unchanged from before: deferred unless TF-IDF precision proves inadequate.
Kalshi mirrors are a fallback execution path, not the product.

---

## Why this ordering

The success criteria for Phase 4 are: (1) surface bot-like profitable wallets
beyond the top-50 seeds, (2) distinguish fresh/high-conviction entries (done in
Phases 1–3), (3) new BUYs visible within one ingest cycle, (4) explainable
drivers everywhere.

The scheduler is the only feature that converts the existing system from "a
dashboard you remember to refresh" into "a feed that finds things while you
sleep" — and it makes deep discovery (which already exists and already does
subgraph recent-trader discovery) actually run, which is the cheapest possible
answer to criterion 1. Archetype scoring is the classification layer that makes
the resulting wider pool *navigable*: without it, a thousand newly discovered
wallets are just rows; with it, `archetype=systematic` + CLV + recent edge is a
bot screener. Per-category skill is next because it is story B's only remaining
structural gap, and its implementation reuses the Bayesian fit verbatim.
