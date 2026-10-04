# MarketSignalOS — Engineering Handoff Blueprint

**Layout, 2026-09-30:** code lives in `apps/api`, `apps/dashboard` and
`services/polymarket-ingestor`. The Replit-era `.migration-backup/` and `artifacts/`
directories and the Next.js app are gone. Sections below keep the paths that were
current when they were written; `CLAUDE.md` has today's commands.

**Status:** authoritative build reference. Supersedes the "+EV Polymarket Analytics
Engine" handoff blueprint (Celery/Redis/PyTorch/WebSocket architecture). Where this
document and any older plan disagree, this document wins.

**Written:** 2026-09-10, against `main` @ `a90d81b` and `codex/lean-pilot-15-budget`
@ `8fda2e2`. Every "as of today" number below was verified by running the code, not
read from a summary.

**Stage 0 completed, 2026-09-10:** see [the gate attrition report](benchmarks/2026-09-10-gate-attrition.md)
and its JSON/notebook companions. All 833 frozen scores match the shadow benchmark
hash. Zero currently qualify; suspending metadata alone admits **two** wallets,
the same two admitted by suspending all six data gates. CLV minimum 10→5 admits
zero. This is **(A), narrowly coverage-bound for two candidates**, with broad model
and economic failures elsewhere. It does not establish a profitable follower strategy.
Production thresholds and the feed are unchanged. The next research step is bounded
metadata-cause diagnosis for those candidates, subject to the remaining merge and
deployment entry gates. The $15/month target and prepare-before-provisioning rule hold.

**Stage 1 progress, 2026-09-10 local date:** the
[metadata audit](benchmarks/2026-09-10-metadata-coverage.md) finds 537 stale coverage
count/ratio tuples. Deriving coverage from the score's own inputs reduces metadata
failures **701→592** and raises data-trusted wallets **125→226**; qualification
remains **0→0**. The scorer repair and frozen gate replay preserve the legacy
coverage rule. Of 20,560 uncovered wallet-condition pairs, 20,352 already have
market rows and 208 do not. Settlement-proxy semantics and missing-fetch provenance
remain open; see [the implementation runbook](metadata-coverage.md).

**Stage 1 progress, 2026-09-12:** [bounded backfill and attempt receipts](metadata-backfill.md)
now cap each reference backfill at 100 conditions/eight actual HTTP attempts and
persist retry/cooldown/interruption evidence. An isolated current Gamma probe
returns 36 of the frozen candidates' 41 missing IDs; five are absent from both
filtered responses. This does not determine historical fetch causes or repair
frozen scores. Coverage/resolution semantics remain open. Validation: 596 tests
passed, one Windows skip; lint and root type checks passed on both platforms.

**Fresh start and the CLV input, 2026-09-29:** the laptop-era local data is retired;
collection restarts in the cloud. Three findings change how Stages 0–1 read:

- **Gate 13 could not be passed on the frozen snapshot.** Price snapshots, CLV's only
  input, began 2026-06-11; the snapshot was taken 2026-06-12 03:01 UTC. That is about
  1.5 days of prices against 4.5 years of activity, and 632 of the 805 gate-13 failures
  were too few observations. Suspending only gates 6 and 13 admits 22 wallets under
  the current prior (at most 154 under any prior). Stage 0's classification is
  therefore **untestable on this snapshot**, not (A) or (C).
- **The prior variance floor is real but cannot explain the empty feed.** Every wallet
  also fails a gate that no prior changes, so the qualifier count is 0 under any prior.
  See [the prior-floor diagnostic](benchmarks/2026-09-28-prior-floor.md).
- **Closing prices are recoverable.** The [live probe](benchmarks/2026-09-29-price-history-probe.md)
  found that CLOB `/prices-history` with an explicit `startTs`/`endTs` window returns
  1-hour points for every probed market closed in 2023 or later, the last point at most
  1.5 h before close. `interval=max` hides this (nothing below 12 h). Markets from
  2021–2022 have no order-book history. So no price-snapshot collector is needed:
  `closing_lines.py` backfills each resolved market's pre-close window into an
  append-only, bitemporal store (§8). Bets on pre-2023 markets must be excluded from
  CLV with a recorded reason rather than counted as missing.
- **But the last pre-close price is the outcome, so it cannot be gate 13's closing
  line.** In the [follow-up](benchmarks/2026-09-29-price-history-probe.md#follow-up-2026-09-30-the-last-pre-close-price-is-the-outcome),
  38 of 40 resolved markets last traded within 0.01 of 0 or 1. CLV against that price
  mostly restates whether a bet won, which gates 8 and 9 already measure, so wiring
  it in would loosen gate 13 (invariant 1). The flaw was latent in forecast-v4 too.
  Scoring does not read the backfill.
- **Gate 13 will measure CLV against the price a fixed time after entry** (open
  decision 6, §12, decided 2026-09-30). The worker backfills every hourly price in the
  six hours after each buy (`entry_prices.py`; the week after it until 2026-10-03), so
  a diagnostic on pilot data can choose the horizon. It also keeps collecting the pre-close windows at a lower cadence, as the
  comparison that diagnostic needs.
- **Polygon logs are complete but not cheaper, and the Goldsky subgraph is gone.** The
  [chain probe](benchmarks/2026-09-29-polygon-logs-probe.md) decoded 30 of 30 Data API
  trades exactly from V2 `OrderFilled` events. It also measured about 2.9 GB a day of
  raw RPC data to follow every trade, and about 970 calls for one wallet's V2 history
  (free nodes cap a query at 10,000 blocks). 2025 history is already pruned on free
  nodes. The orderbook subgraph behind recent-trader discovery now returns
  `ENDPOINT_DEPRECATED`. See Stage 4.

**How to use this.** Stages run in order. Each stage has an **entry gate** — a
condition that must already be true — and **acceptance evidence** — an artifact a
reviewer can inspect to confirm the stage is done. Do not start a stage whose entry
gate is unmet. Do not declare a stage done without producing its evidence file. If a
stage's evidence contradicts this document, update this document in the same commit.

**Companion documents.** `docs/research-credibility.md` (evidence standards),
`docs/platform-roadmap.md` (target architecture), `docs/lean-pilot.md` (budget
worker), `docs/storage-benchmark.md` and `docs/enrichment-shadow.md` (storage
evidence) are on `main` since PR #36 merged on 2026-09-28.

---

## 1. Thesis

MarketSignalOS is a **public research instrument**, not a trading system. It answers
one question:

> Does a frozen, pre-declared rule for selecting Polymarket wallets identify future
> positions that beat a price-based baseline **after realistic follower costs**?

Everything else — the dashboard, the leaderboard, the ledger, the fast lane, the
metrics stack — exists to make that question answerable and the answer auditable. A
well-measured **null result is a successful outcome** of this project. A saturated
score with no prospective validation is a failure, however good the dashboard looks.

Two claims this system will never make: that it has identified insider trading, and
that a wallet's skill probability is the probability its next trade wins. Both are
unprovable from the available data and both are already prohibited in `CLAUDE.md`.

---

## 2. Ground truth as of 2026-09-10

Verified by checking out `codex/lean-pilot-15-budget` and running the suites.

| Check | Result |
|---|---|
| `apps/api` ruff + `mypy .` (CI-equivalent) | clean, 50 files |
| `services/polymarket-ingestor` ruff + `mypy src` (CI-equivalent) | clean, 19 files |
| `apps/web` `npm ci` → `lint` → `build` | exit 0 |
| `pytest apps/api services/polymarket-ingestor` | 518 passed, 11 failed, 6 errors |
| Repo-root `mypy .` (documented in `CLAUDE.md`) | **7 errors on branch, 0 on `main`** |

All 17 test failures share one root cause: `activity_parquet.connect()` sets DuckDB's
`TimeZone` option, which requires the `icu` extension, which DuckDB autoloads over
HTTP from `extensions.duckdb.org`. Any environment without egress to that host fails
closed. See §11.

### The blocking discovery

`docs/benchmarks/2026-09-08-enrichment-shadow.md` reports, on a frozen 16.9M-observation
snapshot covering 2022-01-06 → 2026-06-12:

- 833 wallets scored
- **125 trusted, 708 untrusted**
- **701 flagged "incomplete market metadata"**
- 799 fail "conservative edge is not positive"
- 632 have fewer than 10 closing-line observations
- **0 wallets qualify as tailable**

Read `skill_computation.py:586-638` before interpreting that. Tailability is a
conjunction of thirteen conditions, and it opens with:

```python
tailability_reasons = list(data_reasons)   # skill_computation.py:606
```

`data_reasons` holds six **data-completeness** flags, one of which is
`hydration.metadata_coverage < 1.0`. So a wallet with incomplete market metadata is
blocked from the feed **regardless of its measured skill**. The scorer nevertheless
computes model outputs and all applicable rejection reasons for these wallets.

**Stage 0 corrected the original coverage-only interpretation.** There are 125
data-trusted wallets and 708 with at least one data failure. Of the 701 metadata
failures, 673 also fail the positive conservative-edge gate. Only **two wallets**
fail metadata alone; **706** fail both data and model/economic gates, and all 125
data-trusted wallets fail at least one model/economic gate. Therefore coverage is
the sole recorded obstacle for two candidates, not an explanation for most rejected
wallets. Removing gate checks cannot estimate how repairing inputs would change
the scores. Both complete coverage and prospective evaluation remain necessary.

### The full gate list

For Stage 0 you need these by name. All thirteen must pass for `tailability_status ==
"tailable"` (`skill_computation.py:586-638`):

| # | Gate | Source | Kind |
|---|---|---|---|
| 1 | `activity_history_complete` | hydration | data |
| 2 | `positions_complete` | hydration | data |
| 3 | `closed_positions_complete` | hydration | data |
| 4 | `economic_all_time_complete` | hydration | data |
| 5 | `economic_month_complete` | hydration | data |
| 6 | `metadata_coverage >= 1.0` | hydration | data |
| 7 | `ess >= 20.0` | lifetime fit | model |
| 8 | `posterior_skill >= 0.80` | lifetime fit | model |
| 9 | `edge_lower_bound > 0.0` | lifetime fit | model |
| 10 | `all_time_pnl > 0` and `all_time_roi > 0` and `pnl_30d >= 0` | economics | economic |
| 11 | `recent_ess >= 5.0` (`MIN_RECENT_INDEPENDENT_EVENTS`) | recency fit | model |
| 12 | `recent_fit.edge_mean >= 0.0` | recency fit | model |
| 13 | `clv_sample >= 10.0` (`MIN_CLV_SAMPLE`) and `clv_lower_bound > 0` | CLV | model |

Gates 1–6 are **fixable by engineering**. Gates 7–13 are **findings about the world**.
Conflating them is what produced the current situation.

---

## 3. Invariants

These hold at every stage. A change that violates one is wrong regardless of what it
enables.

1. **Never loosen a gate to populate the feed.** An empty feed is a result. A feed
   populated by moving a threshold is a lie with a UI. If a gate is wrong, change it
   because a *diagnostic* showed it was miscalibrated, in its own commit, with the
   before/after cohort counts recorded.
2. **Raw observations are append-only and immutable. Derived scores are versioned and
   disposable.** You must be able to delete every score and rebuild it. You must never
   be able to overwrite an observation.
3. **Everything is bitemporal.** Every stored row carries `event_time` (when it
   happened) and `observed_time` (when you learned it). Point-in-time reconstruction is
   impossible without both, and without point-in-time reconstruction no evaluation
   and no ML is valid. This is the single most important schema rule in this document.
4. **Deterministic before probabilistic.** Every score must be reproducible bit-for-bit
   from the same inputs and a recorded code hash. `score_snapshot.py` already enforces
   this for the two derived files; extend the contract, don't weaken it.
5. **Separate "computation completed" from "inputs are fresh" from "coverage is
   complete."** A successful scoring run says nothing about whether the data under it
   was current or whole. `platform_status.public_status()` already distinguishes these;
   keep them distinct everywhere.
6. **Non-accusatory language, enforced at the type level where possible.** "observed
   wallet", "historical forecasting evidence", "systematic trading pattern". Never
   "sharp", "insider", "alpha trader" in schema, API, or UI. Naming a mechanism you
   cannot observe makes you stop measuring the one you can.
7. **Follower economics, not wallet economics.** Every EV number is computed at the
   price a follower could actually transact at, on the executable side of the book,
   after delay and fees. The wallet's own PnL is a different quantity and must never
   be presented as the tail's expected return.
8. **One writer per data directory.** JSONL append/index/checkpoint writes are not one
   transaction. Until Stage 6, exactly one process writes at a time, enforced by an OS
   lock. This is why `lean_pilot.worker_lock` exists.
9. **Budget targets are allocations, not caps.** Nothing in this repo configures a
   billing limit. Never describe a design target as a cost guarantee.

---

## 4. Corrections to the original blueprint

Recorded with reasons, because a plan that states the right design without recording
why the obvious-looking design is wrong will be "improved" back into the wrong design.

### 4.1 The consensus fair-value formula is dimensionally wrong — do not build it

The original blueprint proposed:

```
P_model = Σ(Balanceᵢ × E[θᵢ]) / Σ Balanceᵢ ,   where E[θᵢ] = αᵢ / (αᵢ + βᵢ)
EV      = (P_model × Payout_net) − (1 − P_model)
```

`E[θᵢ]` is a **wallet-level skill rating** on [0,1] — how often wallet *i* is right.
`P_model` is supposed to be **P(this specific outcome resolves YES)**. A balance-weighted
average of skill ratings is not that quantity.

Concretely: if every wallet holding a contract has `E[θ] = 0.66`, the formula returns
a fair value of 66¢ whether the market trades at 5¢ or at 95¢, and whether they hold
YES or NO. It ignores the price entirely. Fed into the EV formula it produces confident
nonsense at both tails, and it produces it most confidently exactly where mispricing
claims are least plausible.

**Build this instead** — and note it already exists in
`apps/api/src/marketsignalos_api/services/skilled_bets.py:344`:

```python
fair_price = sigmoid(logit(entry_price) + tail_edge)
tail_ev    = fair_price − executable_price
```

The difference is structural: the correct construction starts **from the market price**
and applies a measured, bounded shift to it. The market price is the strongest single
predictor available and the model's job is to say how far a specific wallet's entry
should move it — not to replace it. Any future fair-value work must preserve this
property: *no estimator that can return a price without reading a price.*

### 4.2 Model-weighted Bayesian updates destroy the interval — do not build them

The original blueprint proposed scaling the Beta-Binomial evidence counts by a neural
model's confidence:

```
α_updated = α₀ + (w_m · I_early_entry)
β_updated = β₀ + (w_m · I_incorrect)
```

The reason to have a Bayesian layer at all is that its credible interval means
something: it tells you how much you should distrust a small sample. Multiplying the
evidence counts by an unvalidated model's output makes the posterior variance a
function of that model. You now have an interval whose width you cannot interpret, and
you have coupled your only calibrated component to your least calibrated one.

**What is already implemented and is the correct treatment of the same underlying
problem** (`skill_computation.py:23-30`, `bayesian_skill.py`):

```
y_i ~ Bernoulli(q_i),   logit(q_i) = logit(p_i) + edge_w
edge_w ~ Normal(mu_pop, sigma2_pop)          # empirical Bayes across wallets
skill_likelihood = P(edge_w > 0 | data)
```

where `p_i` is the market-implied probability at the wallet's volume-weighted entry.
The real threat to this posterior is not miscalibrated confidence — it is **correlated
observations**: a wallet's forty fills across one election are not forty independent
tests of skill. That is what `independent_settled_events` (ESS) exists to penalize, and
it is deterministic, explainable, and auditable. Keep it. If you want a model-derived
input, it belongs as a *feature* in a separate scored layer, never as a multiplier on
evidence counts.

### 4.3 Infrastructure the blueprint specified that you should not build

| Blueprint component | Verdict | Reason |
|---|---|---|
| Celery + Redis worker pipeline | **Do not build** | You have one hourly job. Redis alone is ~⅔ of the entire monthly budget. `lean_pilot.py` already provides leases, deadlines, resource guards, and crash accounting in one locked subprocess. Revisit only when you have ≥3 genuinely concurrent job classes with independent retry semantics. |
| Redis feature store | **Do not build** | There is no online inference path and no latency requirement that a file read fails to meet. |
| CLOB WebSocket ingestion | **Defer indefinitely** | Justified only if you are racing execution. You are not: this is a research instrument, and the fast lane's ≥30s HTTP poll is already faster than the decision loop it feeds. |
| PyTorch LSTM / Transformer | **Defer to Stage 7** | Sequence models need point-in-time features (Stage 5) and a validated non-null baseline (Stage 3). Building them earlier guarantees leakage and unfalsifiable results. |
| Sub-100ms REST / WS streams | **Do not build** | At current scale the dashboard is server-rendered with a 10s upstream timeout and nobody notices. Optimizing this is pure cost. |
| "Insider alpha" framing | **Prohibited** | See invariant 6. |

### 4.4 The one thing the blueprint had right that is still missing

**ERC-1155 transfer decoding from Polygon logs.** Not for the ML — for the
**denominator**.

Discovery today is leaderboard-shaped: wallets enter the population *because they
already performed well*. That is textbook survivorship selection, and it makes the
entire cohort uninterpretable no matter how good the Bayesian fit is. You cannot
compute a false-discovery rate without knowing how many wallets you screened, and you
cannot know that from a leaderboard.

A chain-log listener is the only way to observe the losing and inactive wallets. This
is Stage 4, and it ranks **above** the Postgres migration.

### 4.5 Schema errors in the blueprint's DDL

The proposed schema stores **mutable current state** where it must store **immutable
observations**:

- `condition_tokens.current_polymarket_price` — a mutable price column on the token
  row. Updating it destroys the price history that CLV, `move_captured_pct`, and every
  point-in-time feature depend on. You already learned this the hard way: the
  deduplicating markets store discarded history, which is why
  `polymarket_price_snapshots.jsonl` had to be added.
- `wallet_positions.current_balance` + `last_updated_block` — one mutable row per
  (wallet, token). You cannot reconstruct what a position looked like at a past cutoff,
  so you cannot evaluate a frozen cohort, so you cannot run the experiment this system
  exists to run.
- `sharp_wallets.alpha_score_prior/posterior` — mutable score columns. Scores are
  versioned artifacts (model version × input version × as-of cutoff), not attributes
  of a wallet.
- `sharp_wallets` as a table name, and `is_flagged_algorithmic BOOLEAN` — bakes the
  prohibited framing and a false binary into the schema. `trader_style.py` already
  produces a continuous `automation_score` with five explainable driver strings.

Corrected schema in §8.

---

## 5. The mathematical core

Implement nothing here from memory; these are the definitions the code must match.

**Entry price.** For each (wallet, condition_id, outcome_index), `p_i` is the
**buys-only volume-weighted average price**. Buys-only matters: a wallet that fully
exits before resolution must retain its original entry for CLV. See `_Position`
(`skill_computation.py:96`), which keeps `bought_size`/`bought_cost_usdc` separate from
net accumulators.

**Resolved bet.** A position with non-zero `net_size` at resolution. Positions fully
exited before resolution are not bets — they are realized PnL and are scored by CLV
instead.

**Skill fit.** Hierarchical Bayesian logistic edge, fit jointly across all wallets in
one enrichment pass:

```
logit(q_i) = logit(p_i) + edge_w
y_i ~ Bernoulli(q_i)
edge_w ~ Normal(mu_pop, sigma2_pop)        # empirical Bayes, prior clamped
```

`edge_w` is an additive shift in **log-odds**, which is why it composes with any entry
price without leaving [0,1]. Outputs: `edge_mean`, `edge_lower_bound` (conservative),
`posterior_skill = P(edge_w > 0 | data)`.

**Effective sample size.** `independent_settled_events` down-weights bets sharing an
event so correlated fills cannot inflate confidence. This is the load-bearing defense
against the most common failure mode in this entire domain. Any change to it requires
a written justification in `docs/fix-lessons-learned.md`.

**Recency fit (`forecast-v3`).** The same fit with likelihood weights decayed at a
180-day half-life anchored to the newest bet in the dataset. Gates 11–12.

**Category fit (`forecast-v4`).** Partial-pooling refit per Gamma category using the
wallet's lifetime posterior as prior. A category with ≥5 independent events caps
`tail_edge_used`; a thinner one falls back to the wallet-level score.

**Closing-line value.** Event-capped, capital-weighted mean of
`(closing_or_current_line − buys_only_entry_VWAP)`. Resolved markets count only when a
pre-close price observation exists. CLV is the cleanest mechanism-independent evidence
of edge because it does not require resolution and does not depend on the wallet being
right — only on the market moving toward the wallet's entry. This is why it *gates*
tailability rather than decorating a row.

**Tail fair value and EV.**

```
tail_edge      = min(lifetime edge_lower_bound, category edge_lower_bound if proven)
tail_fair_price = sigmoid(logit(entry_price) + tail_edge)
executable_price = ask           if the Gamma book is known      → tail_ev_source="ask"
                 = 1 − bid       for a NO-side tail
                 = mark          otherwise                        → tail_ev_source="mark"
tail_ev        = tail_fair_price − executable_price               # $/share
```

`_TAIL_EV_MARGINAL_BUFFER = 0.02` separates `positive` from `marginal`. Rows rank by
`tail_ev` **within** each `remaining_edge_status` tier, never across tiers.

**What is still missing from the EV number, and must be added before any performance
claim** (Stage 3): entry delay between wallet fill and follower fill, slippage at
realistic size, Polymarket fees, and an explicit exclusion policy for observations with
no executable quote. Missing quotes must be **excluded with a recorded reason**, never
imputed as zero cost.

---

## 6. Stage plan

Each stage: **Question → Entry gate → Build → Acceptance evidence → Non-goals.**

---

### Stage 0 — Diagnose the gate attrition

**Question.** Of the thirteen tailability gates, which ones are actually binding, and
is the empty feed a coverage artifact or a finding about alpha?

**Entry gate.** None. Start here. This is an afternoon of work on data you already
have and it determines whether any later stage is worth doing.

**Build.** A diagnostic command — `marketsignalos_polymarket.gate_attrition` — that
reads a frozen snapshot and emits a **waterfall**: starting from N wallets, how many
survive each gate applied cumulatively in the order of §2, and how many fail each gate
in isolation. Emit both orderings; the cumulative one shows what is binding, the
isolated one shows what is fixable.

Then run three counterfactual passes, **as diagnostics only, never shipped**:

1. Data gates (1–6) suspended. How many wallets clear gates 7–13?
2. Gate 6 (`metadata_coverage`) alone suspended. Isolates the 701.
3. Gate 13 (CLV sample ≥10) relaxed to 5. Tests whether the CLV gate is calibrated for
   a sample size your coverage cannot currently produce.

**Acceptance evidence.** `docs/benchmarks/<date>-gate-attrition.md` + `.json`, with the
waterfall table, the three counterfactual counts, the frozen snapshot's SHA-256, and a
one-paragraph conclusion naming which of these three worlds you are in:

- **(A) Coverage-bound candidates** — some wallets clear model/economic gates but
  fail only coverage. → Stage 1 diagnoses those candidates; report how many, rather
  than assuming the whole cohort is coverage-bound. Prospective skill is still untested.
- **(B) Calibration-bound** — data gates suspended, wallets still fail 7–13 narrowly
  and in a patterned way (e.g. everyone fails only gate 13). → One gate is calibrated
  for a sample size you don't have; fix that gate specifically, with this evidence
  cited in the commit.
- **(C) No qualifying signal in the observed sample** — data gates suspended, no
  wallets clear 7–13 and failures are broad rather than near one threshold. → Review
  §10. This is a scoped result under the current model and observed inputs, not proof
  that the wider population has no edge or that repaired data would leave scores unchanged.

**Non-goals.** Do not change any threshold in this stage. Do not deploy anything. Do
not touch the feed. Changes are limited to the diagnostic command, its tests,
reproducible evidence, and the verification repairs needed to review this branch.

---

### Stage 1 — Close the coverage blocker

**Question.** Can `metadata_coverage` reach 1.0 for the wallets you care about, and if
not, is a hard `< 1.0` block the right semantics?

**Entry gate.** Stage 0 concluded (A) or (B). If (C), skip to §10.

**Build.**

1. Review the integrated `codex/lean-pilot-15-budget` after clearing §11. Its bounded
   collection controls are already on this feature branch. Per the user's request,
   continue isolated diagnostics and reviewable repairs there; main-branch merge
   and cloud promotion remain separate delivery steps.
2. Instrument *why* `metadata_coverage < 1.0`: which condition_ids referenced by
   activity have no row in `polymarket_markets.jsonl`, and why the backfill missed
   them (never fetched / Gamma 404 / category absent / pagination cap). Emit counts by
   reason — you cannot fix a coverage number you cannot decompose.
3. Fix confirmed causes. The 2026-09-10 pass repairs stale coverage inside scoring.
   The remaining largest bucket is present-but-unsettled market records, requiring
   an explicit coverage-contract decision. `/positions` pagination remains separate
   work; it is not an input to this activity-condition coverage denominator.
4. **Reconsider the semantics.** A hard block at `< 1.0` means one unresolvable market
   in a 4-year history permanently disqualifies a wallet. Consider a graded contract:
   coverage ≥ some threshold with the uncovered fraction *excluded from the fit and
   reported*, rather than the wallet excluded from the product. If you make this
   change, it must be justified by the Stage 0 waterfall and land in its own commit
   with before/after cohort counts. This is the one gate change this document
   pre-authorizes, and only on that evidence.

**Acceptance evidence.** A re-run of the Stage 0 waterfall on the same frozen snapshot
showing `metadata_coverage` failures dropped from 701, with the decomposition-by-reason
table before and after. Trusted-wallet count moves from 125 to a stated number.

**Non-goals.** Do not add new discovery sources here. Do not touch gates 7–13.

---

### Stage 2 — Deploy bounded unattended collection

**Question.** Can collection run for weeks without your laptop, inside a stated budget,
without corrupting its own store?

**Entry gate.** Stage 1 evidence exists. `recovery_required` semantics understood.

Deployment is sequenced here — before evaluation, not after — for one reason: a
prospective evaluation needs a **clock that keeps running**. If collection stops
mid-window the experiment dies and cannot be restarted without a new cutoff. The lean
pilot is the enabler for Stage 3, not a reward for finishing it.

**Build.** Deploy `lean_pilot` as a Railway scheduled worker per `docs/lean-pilot.md`,
with its own volume, separate from the serving API. Before provisioning:

- Resolve the raw-collection restart-safety item the pilot doc names as next: the
  in-memory activity dedupe index does not shrink with wallet batching and is the
  standing OOM risk. Either make it transactional/on-disk or prove the 4 GiB guard
  holds on a real batch — measured, not assumed.
- Define a retention policy. Score generations are ~426 MB each and nothing currently
  deletes them.
- Write and test the recovery procedure for `recovery_required`. There is deliberately
  no automatic repair; there must be a documented manual one.
- Set a Railway alert at $10 and a workspace compute limit at $15, and record in the
  runbook that the hard limit takes *all* workspace workloads offline.

**Prepared 2026-09-29, not provisioned.** `deploy/worker.Dockerfile` packages the worker
with production dependencies from `uv.lock`, and the root `railway.toml` runs it
as an hourly Railway cron service. CI builds the image and checks that plan mode runs
without network access, that the worker refuses to start without a volume, and that
the supervisor works inside the image. `psutil`, which the supervisor imports, was
only a dev dependency until this change, so a production `--run` would have crashed.
See `docs/railway-deployment.md`. Of the items above, a fresh volume only defers the
dedupe-index risk (the index starts empty; the receipts' peak RSS tracks its growth).
Retention, the recovery procedure and the billing alert are still open.

**Acceptance evidence.** `docs/benchmarks/<date>-pilot-live.md`: 14 consecutive days
of receipts with laptop off; measured peak RSS, CPU-seconds, and wall time per cycle
against the 4 GiB / 20-minute / 60-minute-per-day guards; one deliberately killed
worker showing the reservation retained and `recovery_required` set; one successful
recovery; and the **actual Railway invoice line** against the $15 target.

**Non-goals.** Do not switch API reads to the pilot's snapshots yet (that is Stage 5).
Do not enable the API's own scheduler simultaneously — one ingestion owner.

---

### Stage 3 — Freeze cohort v1 and start the prospective clock

**Question.** Does the current selection rule beat baseline going forward?

**Entry gate.** Stage 2 running for ≥14 days with no unexplained gaps.

Run this on the **leaderboard-discovered cohort**, knowing it is survivorship-biased,
and say so in the evidence. Selection on past success can inflate retrospective
performance, but it does not establish an upper bound on future performance for
other cohorts. Cohort v1 is a cheap first prospective test. A null result can support
a predeclared budget decision to stop; it cannot prove an unbiased cohort would fail.
Do not wait for perfect discovery to start the clock.

**Build.** Per `docs/research-credibility.md`, freeze and commit *before* looking at
any outcome:

- Discovery source, cutoff timestamp, and **every screened wallet** including losers
  and inactives, each with its qualification/exclusion reason.
- Model version, prior version, training interval, exact gate thresholds. Frozen for
  the whole evaluation. Every variant you tried, recorded.
- **One** primary outcome and horizon. Recommended: event-weighted signed price
  improvement over a pre-declared horizon using contemporaneous executable quotes.
  Settlement ROI and calibration are secondary.
- A price-only baseline and a matched comparison cohort selected using only
  information available at the cutoff. State the matching procedure and the residual
  imbalance.
- Follower cost model: entry delay, executable side, size, spread, fees, slippage.
  Wallet PnL kept strictly separate from follower PnL. Non-executable observations
  excluded **with reasons**, never zero-filled.
- Event-cluster resampling, a fixed evaluation date, a censoring policy for unresolved
  events, and a multiple-comparison policy for any per-wallet claim.

The existing `signal_ledger` is the right substrate — it already freezes surface-time
prices and book context per row. Extend it with the cohort ID and the frozen config
hash; do not build a parallel mechanism.

**Acceptance evidence.** `docs/evaluations/cohort-v1/frozen-config.json` committed
**before** the window opens, with its hash recorded in this document. At the evaluation
date, `docs/evaluations/cohort-v1/result.md`: primary outcome with confidence
interval, calibration plot, Brier score, log loss, the ablations (price-only, CLV-only,
historical-edge, combined), and every null and failed variant.

**Non-goals.** No model changes during the window. No gate changes during the window.
No peeking — if you look early, you have a sequential-testing problem and must say so.

---

### Stage 4 — Unbiased discovery via Polygon chain logs

**Question.** What does the whole population look like, not just the winners?

**Entry gate.** Stage 3 window open (this runs in parallel — it feeds cohort v2).

**Build.** The one component from the original blueprint worth building. A Polygon
log listener decoding ERC-1155 `TransferSingle`/`TransferBatch` on the Polymarket
conditional-token contract, with:

- A **durable cursor** (block number + log index), the gap the roadmap names as the top
  discovery deficiency.
- **Reorg handling.** Store block hash; re-scan on divergence; mark superseded rows
  rather than deleting them (invariant 2).
- Bitemporal rows (invariant 3): `event_time` from the block, `observed_time` from
  ingestion.
- Object-storage partitions for raw logs; Postgres for normalized facts and cursors.

Also documented semantics — and tests — for split, merge, redemption, and fee events.
A JSONL→Parquet export of Data API observations is **not** a raw chain archive and must
not be described as one.

**Acceptance evidence.** A wallet census over a stated block interval with the count of
wallets never seen by leaderboard discovery; a demonstrated reorg recovery; and a
cohort-v2 candidate list whose screened population includes losers and inactives.

**Non-goals.** Do not replace Data API hydration with chain data. Chain logs give you
the population; the Data API gives you enriched history. You need both.

**Probe, 2026-09-29.** [Measured on live public infrastructure](benchmarks/2026-09-29-polygon-logs-probe.md),
this non-goal holds:

- **Decoding works.** The exchange `OrderFilled` events decode to the Data API's
  trades exactly (30 of 30). Since April 2026 they come from the V2 exchanges,
  whose event layout differs from V1.
- **Use `OrderFilled` for the trade population.** Those events carry side and price,
  which ERC-1155 transfers do not. Keep the ERC-1155 decoding for splits, merges and
  redemptions only.
- **Following the head is affordable on a free node; backfilling is not.** Following
  new blocks costs up to 2.9 GB a day. A free node caps a query at 10,000 blocks,
  so one wallet's V2 history takes about 970 calls. 2025 logs are already pruned.
- **Plan for a paid archive RPC or indexer, but not yet.** Any on-chain history
  before 2026 needs one, as a line item in §9, decided after Stage 3.
- **Chain discovery now fills a gap.** The Goldsky orderbook subgraph that supplied
  recent-trader discovery is shut down (`ENDPOINT_DEPRECATED`). A head-following
  `OrderFilled` reader is its replacement.

---

### Stage 5 — Point-in-time serving contract

**Question.** Can a reader be guaranteed a consistent, versioned, explicitly-stale-able
view?

**Entry gate.** Stage 2 stable.

**Build.** `score_snapshot.py` already publishes validated, hash-manifested, atomically
pointer-swapped score generations — and **`load_current()` currently has zero
callers**. Close that loop:

1. Extend the snapshot beyond the two derived files to a **complete serving snapshot**:
   positions, market metadata, coverage statistics, and *separate* source-time and
   score-time timestamps.
2. Switch API reads to a pinned generation, with parity tests against the legacy JSONL
   path, a rollback procedure, and an explicit stale state surfaced to the UI.
3. Keep `polymarket_price_snapshots.jsonl` semantics — append-only price observations
   are the substrate for CLV and every point-in-time feature.

**Acceptance evidence.** Parity test showing the pinned-generation feed byte-identical
to the legacy feed on a frozen input; a demonstrated rollback; the dashboard showing
an explicit stale state when the pointer is old.

**Non-goals.** Do not migrate storage engines here. The Parquet work measured 287×
faster queries and 7.4× smaller storage but **no RSS improvement** (3,146 → 3,214 MiB) —
it does not solve the memory ceiling and is not the production path yet.

---

### Stage 6 — Postgres authoritative

**Question.** Can the API run with its JSONL directory absent?

**Entry gate.** Stage 5 complete. Stage 3 returned a non-null (see §10).

**Build.** Per `docs/platform-roadmap.md` §2. Replace JSONL reads in feed, leaderboard,
dossier, scoring, ledger, exits, and notification watermarks. Convert dual-writes to
transactional writes with stable unique keys. Persist job leases, cursors, attempts, and
completion state. Split API from worker **only after both read the same authoritative
database**. Indexed pagination, not whole-history loads.

**Acceptance evidence.** API serves correctly with the JSONL directory renamed away; a
killed worker restarts with no duplicates and no skipped committed work; per-table row
reconciliation against the JSONL source; measured query latency and worker peak memory.

**Non-goals.** No message broker. A jobs table with leases and retries first — revisit
only if that measurably fails.

---

### Stage 7 — Machine learning

**Question.** Do learned features beat the deterministic baseline on unseen later data?

**Entry gate.** **Stage 3 returned a non-null result.** If cohort v1 and cohort v2 both
show no edge over baseline after costs, this project's budget policy cancels the ML
stage. That is a prioritization decision, not proof that no learnable pattern exists.

**Build.** In this order, stopping as soon as a step fails to beat the previous one:

1. Price-only baseline.
2. Logistic regression on point-in-time features from Stage 5 snapshots.
3. Gradient-boosted trees.
4. Only then, sequence models.

Chronological splits. Group related events so correlated outcomes cannot leak. Prevent
future wallet selection from leaking into earlier training rows — this is the leakage
that makes most published results in this domain worthless. One final holdout, never
touched. Save feature definitions, dataset hashes, training config, and artifacts.
Promote only after later-unseen evaluation **and** a shadow run.

**Acceptance evidence.** Ablation table against the deterministic baseline on the
untouched holdout, with the same cost model as Stage 3.

**Non-goals.** No distributed training. No online inference. No model in the tailability
path — invariant 4 stands, and a learned score may inform ranking but never gates.

---

## 7. Architecture, corrected

```mermaid
flowchart TD
    CH[Polygon ERC-1155 logs<br/>durable cursor + reorg handling] --> RAW[Raw event collector]
    DA[Polymarket Data API<br/>activity / positions / economics] --> HY[Bounded hydration worker]
    GM[Gamma markets + price/book] --> MK[Market + quote collector]

    RAW --> OBJ[(Object storage<br/>immutable partitions)]
    RAW --> DB[(Postgres<br/>normalized facts + job cursors)]
    HY --> DB
    MK --> DB

    DB --> SC[Deterministic scoring worker<br/>forecast-v3 / v4 + CLV]
    SC --> V[Versioned score generations<br/>manifest + pointer swap]
    V --> API[Read-only FastAPI]
    API --> WEB[Public Next.js site]

    DB --> DS[Point-in-time feature snapshots]
    OBJ --> DS
    DS --> EV[Offline evaluation<br/>frozen cohort, walk-forward]
    EV -.gate.-> ML[ML experiments — Stage 7 only]
```

Differences from the original blueprint: no broker, no feature-store cache, no
WebSocket ingestion, no inference in the serving path, and an explicit gate between
evaluation and ML.

---

## 8. Data contracts

Postgres shapes for Stage 6. **Every table obeys invariants 2 and 3.**

```sql
-- Observed wallets. No scores here: scores are versioned artifacts (below).
CREATE TABLE observed_wallets (
    wallet_address      CHAR(42) PRIMARY KEY,
    first_observed_at   TIMESTAMPTZ NOT NULL,
    last_observed_at    TIMESTAMPTZ NOT NULL,
    discovery_source    TEXT        NOT NULL,   -- 'leaderboard' | 'chain_log' | 'manual' | 'recent_trader'
    discovery_cutoff_at TIMESTAMPTZ NOT NULL    -- selection-bias provenance; required
);

-- Market/outcome identity. NO mutable price column.
CREATE TABLE condition_tokens (
    token_id        NUMERIC(78,0) PRIMARY KEY,
    condition_id    CHAR(66)     NOT NULL,
    market_slug     TEXT         NOT NULL,
    outcome_label   TEXT         NOT NULL,
    outcome_index   INT          NOT NULL,
    category        TEXT,
    resolved_at     TIMESTAMPTZ,
    winning_outcome_index INT,
    UNIQUE (condition_id, outcome_index)
);

-- Append-only price observations. This is what CLV and every point-in-time
-- feature read. Never UPDATE a row here.
CREATE TABLE price_observations (
    token_id     NUMERIC(78,0) NOT NULL REFERENCES condition_tokens(token_id),
    event_time   TIMESTAMPTZ   NOT NULL,   -- when the price was true
    observed_time TIMESTAMPTZ  NOT NULL,   -- when we learned it
    mark         NUMERIC(9,6)  NOT NULL,
    best_bid     NUMERIC(9,6),
    best_ask     NUMERIC(9,6),
    source       TEXT          NOT NULL,
    PRIMARY KEY (token_id, event_time, observed_time, source)
);

-- Append-only balance observations. Current balance is a VIEW over this,
-- so any past cutoff can be reconstructed.
CREATE TABLE position_observations (
    wallet_address CHAR(42)      NOT NULL REFERENCES observed_wallets(wallet_address),
    token_id       NUMERIC(78,0) NOT NULL REFERENCES condition_tokens(token_id),
    balance        NUMERIC(30,6) NOT NULL,
    block_number   BIGINT,
    block_hash     CHAR(66),
    event_time     TIMESTAMPTZ   NOT NULL,
    observed_time  TIMESTAMPTZ   NOT NULL,
    superseded_by  BIGINT,                  -- reorg: mark, never delete
    PRIMARY KEY (wallet_address, token_id, event_time, observed_time)
);

-- Scores are versioned artifacts, not wallet attributes.
CREATE TABLE wallet_score_versions (
    wallet_address    CHAR(42)     NOT NULL REFERENCES observed_wallets(wallet_address),
    as_of             TIMESTAMPTZ  NOT NULL,   -- point-in-time cutoff of inputs
    model_version     TEXT         NOT NULL,   -- 'forecast-v4'
    inputs_version    TEXT         NOT NULL,   -- snapshot manifest sha256
    cohort_id         TEXT,                    -- set when part of a frozen evaluation
    posterior_skill   NUMERIC(9,6) NOT NULL,
    edge_mean         NUMERIC(9,6) NOT NULL,
    edge_lower_bound  NUMERIC(9,6) NOT NULL,
    independent_events NUMERIC(9,3) NOT NULL,
    clv_mean          NUMERIC(9,6),
    clv_lower_bound   NUMERIC(9,6),
    clv_sample_size   INT,
    automation_score  NUMERIC(9,6),            -- continuous, not a boolean flag
    tailability_status TEXT        NOT NULL,
    exclusion_reasons  JSONB       NOT NULL,   -- every failed gate, by name
    PRIMARY KEY (wallet_address, as_of, model_version, inputs_version)
);

-- Job state. This is the broker you don't need.
CREATE TABLE ingestion_jobs (
    job_id        BIGSERIAL PRIMARY KEY,
    source        TEXT        NOT NULL,
    cursor        JSONB       NOT NULL,
    lease_owner   TEXT,
    lease_expires TIMESTAMPTZ,
    attempts      INT         NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    UNIQUE (source)
);

-- Selection-bias provenance for every evaluation.
CREATE TABLE evaluation_cohorts (
    cohort_id       TEXT PRIMARY KEY,
    discovery_rule  JSONB       NOT NULL,
    cutoff_at       TIMESTAMPTZ NOT NULL,
    screened_wallets JSONB      NOT NULL,   -- ALL screened, with per-wallet reason
    config_sha256   CHAR(64)    NOT NULL,
    frozen_at       TIMESTAMPTZ NOT NULL,
    evaluation_date DATE        NOT NULL,
    correction_method TEXT      NOT NULL
);
```

`exclusion_reasons` as JSONB is not decoration: it is what makes the Stage 0 waterfall
reproducible at any point in the future, and what lets you answer "why isn't this
wallet in the feed" without re-running the scorer.

---

## 9. Cost model

Target **$15/month**, Railway Hobby. Rates verified 2026-09-09: Hobby minimum $5
credited toward usage; RAM $10/GB-month; CPU $20/vCPU-month; volumes $0.15/GB-month;
egress $0.05/GB.

| Allocation | Target | Note |
|---|---:|---|
| Scheduled collection worker | $4 | ~$2.50/mo at 4 GB × 1 vCPU × 1 h/day, 30-day normalized |
| Public serving (API + web) | $7 | **Unmeasured.** Not proven to fit. |
| Storage + transfer | $2 | No retention policy exists yet |
| Contingency | $2 | |

These are **allocations, not measured prices** (invariant 9). The local runtime
allowance covers time inside charged cycles, not total billed container life; frequent
invocations still pay startup. Score generations are ~426 MB each with no automated
retention. Nothing in this repository configures a billing limit — set the $10 alert
and $15 workspace compute limit manually, and note in the runbook that the hard limit
takes every workload in that workspace offline.

---

## 10. Definition of done, and kill criteria

**Done** is not "the dashboard is live." Done is:

> A frozen, pre-registered cohort, evaluated at a pre-declared date, on an unbiased
> screened population, with follower costs applied, reported with confidence intervals
> and a multiple-comparison correction — together with the calibration and ablation
> evidence that says whether the effect is the model's or the market price's.

**Kill criteria.** Write these down now, while you have no result, because that is the
only time you can write them honestly. Stop building and publish the null if:

1. Stage 0 concludes **(C) signal-absent** — data gates suspended, wallets fail the
   model gates broadly and by wide margins.
2. Cohort v1 (selected on past performance) shows no edge over the price-only baseline after
   costs, at the pre-declared date.
3. Cohort v2 (unbiased, Stage 4) shows no edge after costs.
4. The effect survives only when the follower cost model is removed.

A published null result on 16.9M observations across 4.5 years, with a reproducible
method, is a genuinely valuable artifact and a better outcome than an unfalsifiable
dashboard. `docs/research-credibility.md` already commits to this. Honor it.

---

## 11. Defects to clear before Stage 1

Originally verified on `codex/lean-pilot-15-budget` @ `8fda2e2`.
The 2026-09-10 Stage 0 pass fixes items 1, 2, and 5; item 4's authenticated
operator runbook is linked from the index. Item 6 was validated locally with Alloy
v1.19.2 using placeholder credentials (no live scrape). Item 3 remains Stage 5 work.

1. **Root `mypy .` regression.** `apps/api/src/marketsignalos_api/api/routes/ingestor.py:269`
   — `run_deep_pipeline(**options, **kwargs)` with `options: dict[str, int]`. Not a
   runtime bug (all four keys exist on the signature) but strict mypy cannot verify
   heterogeneous `**kwargs`. CI runs mypy *from inside* `apps/api` where the ingestor
   is `ignore_missing_imports`, so CI passes while the command documented in
   `CLAUDE.md` fails. `main` is clean at root. Fix with a `TypedDict` or explicit
   keywords. **Then close the CI hole**: add a repo-root `mypy .` job, or CI will keep
   being weaker than the documented local check.
2. **DuckDB fetches an extension over the network at connect time.**
   `activity_parquet.connect()` sets `"TimeZone": "UTC"`, requiring `icu`, autoloaded
   from `extensions.duckdb.org`. 17 tests fail closed in any environment without egress
   to that host, and a locked-down Railway container is such an environment.
   The Stage 0 fix removes this option and explicitly disables extension auto-install
   and autoload. A fresh-process test uses an empty extension directory and confirms
   offset-aware as-of queries agree with JSONL. Event times are epoch integers;
   observation times are `TIMESTAMPTZ`, so both sides of the cutoff are tested.
3. **`load_current()` has zero callers.** Wire it in Stage 5 or the publication
   contract is unexercised in production.
4. **Production has no operator controls.** `showControls` requires
   `NODE_ENV !== "production"`, so the deployed dashboard can never show ingest or
   watchlist controls. Fail-closed is correct absent an auth UI, but document it in the
   runbook as the operator path (bearer token + curl), don't leave it implicit.
5. **`CLAUDE.md` drift.** Eight docs added on that branch, zero registered in the Docs
   table; `DATA_STALE_AFTER_MINUTES`, `INGEST_DEEP_*`, and `SHOW_INGEST_CONTROLS` are
   absent from the env table. `CLAUDE.md` is the index every future session reads first.
6. **`ops/grafana/config.alloy` lost binary validation.** `main` carried a config
   validated against Alloy v1.19.1; the bearer-token edit is unvalidated. Re-run
   `alloy fmt` and `alloy validate` with the token variable supplied. Completed with
   v1.19.2; archive/config hashes and scope are recorded in `docs/gate-attrition.md`.

---

## 12. Open decisions

Answer these before Stage 1; each changes what gets built.

1. **Is `metadata_coverage < 1.0` the right block?** A single unresolvable market in a
   4-year history currently disqualifies a wallet permanently. Graded coverage with the
   uncovered fraction excluded-and-reported is the alternative. Stage 0's waterfall
   decides this. (Pre-authorized under §6 Stage 1 on that evidence only.)
2. **Is `MIN_CLV_SAMPLE = 10` calibrated for the coverage you can achieve?** 632 of 833
   wallets fail it. If post-Stage-1 coverage still cannot produce 10 pre-close price
   observations for most wallets, the gate is measuring your collection cadence rather
   than the wallet. *Answered 2026-09-29:* on the frozen snapshot it measured how long
   prices had been collected (about 1.5 days). Backfilled closing lines remove that
   limit for markets closed in 2023 or later; re-decide the threshold on fresh data.
3. **Does the entity-dedupe heuristic hold?** Consensus counts cluster wallets by
   shared display name. On-chain funding-source clustering is the rigorous upgrade and
   `CLAUDE.md` already labels the current approach a heuristic. It affects any
   consensus-based claim.
4. **What is the primary outcome for cohort v1?** §6 Stage 3 recommends event-weighted
   signed price improvement over a pre-declared horizon. Pick one and freeze it —
   picking after you look is the failure mode this whole document exists to prevent.
5. **Retention.** Score generations at ~426 MB each, raw archive, logs, abandoned
   generations. Nothing deletes anything today. Required before Stage 2 runs unattended
   for weeks.
6. **What is the closing line for a resolved bet?** Not the last price before close:
   that is the outcome (38 of 40 probed markets). Candidates were:
   - the last price at least *L* hours before close;
   - the price *h* hours after entry, which is also defined for open and exited bets.

   *Decided 2026-09-30: the price h hours after entry.* The worker collected the week
   after each buy, so h could be anything up to seven days; since the second
   amendment below it collects six hours. The horizon is still open.
   A diagnostic on pilot data picks it and must report, for each h:
   - how often the reference sits within 0.01 of the outcome, compared with the
     last pre-close price on the same bets;
   - how strongly CLV correlates with winning;
   - how many bets have no reference because the market closed first;
   - the gate 13 pass counts before and after.

   Choose h before looking at gate 13 counts for any wallet. The chosen definition
   becomes a new score version.

   The diagnostic is `horizon_diagnostic.py`, run by the pilot worker. It
   writes `diagnostics/horizon/<date>.json` and prints the same numbers in the
   Railway log line, for h = 1 and 6 hours (1, 6, 24, 72 and 168 until the second
   amendment), per horizon and on the bets referenced at every horizon (the common
   set). It never computes gate
   counts.

   *Selection rule, recorded 2026-10-02 before any report existed and approved
   by the owner the same day; any later change must be justified in writing:*
   wait for at least 500 bets from at least 20 wallets in the common set. Then
   take the longest horizon whose common-set `near_outcome` share is at most
   0.05 (and, since the third amendment, also on every bet referenced at it), provided at least half of the resolved bets whose window has been
   fetched have a reference at it (`coverage`, per horizon) and its common-set
   CLV-win correlation is positive.
   Longer horizons give the market more time to agree with a good entry, so they
   carry more signal; the leakage bound stops that turning into the outcome. The
   correlation is a floor, not a target: picking the horizon that best predicts
   winning would rebuild the circularity this decision removes. If no horizon
   qualifies, post-entry CLV cannot be measured without leakage on this data, and
   gate 13 is redesigned or dropped, with that recorded here.

   The rule is code, not judgement: `horizon_diagnostic.RULE` and
   `select_horizon` apply it to every report (`selection`). The first report whose
   sample meets it is written to `diagnostics/horizon/decision.json` and never
   replaced, so the decision is taken once, at the first sufficient sample, rather
   than whenever a later report looks preferable. Later reports keep showing the
   day's numbers beside the standing `decision`.

   *Amendment, approved by the owner 2026-10-02 after the first report
   (23:10 UTC), before any report was eligible:* only fills placed at least seven
   days before the market's scheduled end count
   (`RULE["min_hours_to_scheduled_end"] = 168`). The reason is feasibility, not the
   numbers: the common set requires a price at every horizon up to seven days, and
   almost no market in the cohort lived that long, so the sample could never reach
   500 and the rule could never decide. The scheduled end is known at entry, so the
   filter adds no leakage; it also keeps the bets a person has time to copy. For the
   record, that report (1,132 resolved bets from 43 wallets, 4.5% of bought markets
   with metadata) showed `near_outcome` 0.16 at 1 h, 0.42 at 6 h and 0.27 at 24 h,
   with coverage falling from 0.88 at 1 h to 0 at 168 h.

   *Cohort change, approved the same day:* the day-volume leaderboard had filled the
   pilot with automation-shaped wallets on markets that close within hours. The
   `cohort` stage (`cohort.py`) now excludes, for good, every wallet the scorer
   labels `systematic` and deletes its data; the watchlist refills from the monthly
   volume leaderboard. The diagnostic's population therefore changes; no decision
   had been recorded before it did.

   *Seeding change, approved by the owner 2026-10-03 (12:40 UTC):* the monthly
   volume leaderboard was no better. Of the 57 wallets the 08:12 score labelled,
   31 were `systematic` (about 31 of 49 new seeds, 63%), and their rows were
   218,620 of the activity store's. The pilot now seeds from the monthly
   **profit** leaderboard (`leaderboard_metric: profit`). Ranking by profit
   selects wallets *because their recent bets won*, and favours one-large-win
   wallets that `run_pipeline`'s default excludes for that reason. The cohort is
   therefore outcome-selected: backward-looking results from it (win rate, skill
   likelihood or CLV on bets already settled) are not evidence of skill, and any
   report built on it must say so. Prospective results (bets placed after a
   wallet entered the cohort) and the horizon diagnostic, which measures market
   convergence rather than wallet skill, are not affected. The 26 volume-seeded
   wallets that survived the purges stay in the watchlist.

   *Seed depth, approved by the owner 2026-10-04 (08:40 UTC):* the pilot read only
   the top 25 of the leaderboard, so once each was watched or excluded nothing new
   could enter, and the watchlist fell from 64 to about 35 while the rule waits on
   20 wallets (17 in the 08:12 report). It now reads the top 100
   (`leaderboard_limit: 100`); the 64-wallet cap still bounds collection. In the
   first profit-seeded score, 10 of 45 wallets were labelled `systematic` (at most
   10 of 19 profit seeds, against 31 of about 49 volume seeds).

   *A scheduled end is not a close (2026-10-03).* The 03:08 UTC report kept 110 bets
   and found no reference for 11 of 12 fetched bets at 24 h and 8 of 8 at 72 h:
   either markets resolving long before their scheduled end, or gaps in thin
   markets' hourly series. Every `no_reference` is now split into `closed` (Gamma
   `closedTime` before the horizon), `ended` and `gap`; the funnel counts kept bets
   whose market actually closed within seven days of entry
   (`bets_7d_closed_within_7d` of `bets_7d_close_known`); and both backfills fetch
   these bets' chunks and markets first (`priority_chunks`, `priority_markets`).
   The 09:12 UTC report answered it: of 274 kept bets with a known close, 261 (95%)
   had closed within seven days, and every missing reference at 24, 72 and 168 h was
   `closed` (gaps: 3 of 329 at 1 h and 6 h). The common set held 5 bets and could
   not reach 500 at any horizon set that included 24 h or longer.

   *Second amendment, approved by the owner 2026-10-03 (09:25 UTC), before any
   report was eligible:* the candidate horizons are 1 h and 6 h
   (`horizon_diagnostic.HORIZONS_HOURS`), and the entry-price backfill collects six
   hours after each buy. The reason is feasibility, as with the first amendment:
   longer horizons have no price for 95% of the bets. Unlike the first, this one
   was made after per-horizon numbers had been seen, so for the record the bound
   and the sample were left exactly as approved (`RULE` is unchanged, including the
   seven-day filter), and the numbers seen were: `near_outcome` 0.064 at 1 h
   (242 bets, 18 wallets) and 0.24 at 6 h (133 bets), against 0.64 and 0.71 for the
   last pre-close price on the same bets; CLV-win correlation 0.33 and 0.50. On
   those numbers neither horizon passes the 0.05 bound, so the likely outcome is
   no horizon and the redesign of gate 13 described above; the rule decides at
   500 common bets from 20 wallets. Filtering on the actual close instead was
   rejected: it uses information from after entry, keeps about 5% of bets, and
   favours markets that resolve at their deadline.

   *Third amendment, approved by the owner 2026-10-03 (12:30 UTC), before any
   report was eligible:* the leakage bound must hold on every bet referenced at a
   horizon as well as on the common set (`select_horizon`). The common set holds
   only bets whose market was still open at the longest horizon, and that
   condition is itself information from after entry: it removes the markets about
   to resolve, so the common set is cleaner than the bets a chosen horizon would
   score. This tightens the rule; nothing was relaxed. It was made after numbers
   had been seen, recorded here: the 12:15 UTC report had 136 common bets from 17
   wallets, with `near_outcome` at 1 h of 0.017 on the common set and 0.0595 on
   all 245 bets referenced at 1 h. Under the second amendment alone the rule would
   have been on course to choose 1 h; under this one 1 h fails on today's numbers,
   and the decision is still taken at 500 common bets from 20 wallets.
