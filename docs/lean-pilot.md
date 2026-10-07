# Lean pilot: $15 monthly design target

Commands below keep the paths current when they were written. The worker now
ships as `deploy/worker.Dockerfile` and runs on Railway through `railway.toml`;
see [railway-deployment.md](railway-deployment.md) and, for today's commands,
`CLAUDE.md`.

Status after the 2026-09-09/10 development pass: implemented locally and tested;
not deployed or measured on Railway. No cloud services, schedules, billing
limits, or paid data subscriptions were created. The current public API does
not yet consume these new score snapshots.

## What this pass built

The new `marketsignalos_polymarket.lean_pilot` command runs due work once and
exits. Collection and scoring have independent, persisted attempt cadences.
Successful completion timestamps are separate from attempts and partial results.
The default configuration is committed in `deploy/lean-pilot.json`:

| Control | Default | Meaning |
|---|---:|---|
| Collection | Every hour | Refresh a rotating subset of eligible wallets. Every stage counts as due up to a quarter of its interval (at most 10 minutes) early, so the cron's :07–:12 drift cannot skip an hour |
| Scoring | Every 24 hours | Recompute derived scores into a new local generation |
| Entry prices | Every 6 hours in `deploy/lean-pilot.json`; off in code | Backfill hourly prices for the six hours after each buy (the week after it until 2026-10-03), in week-long chunks fetched once a chunk has ended; at most 400 chunks and 180 s per run. The horizon diagnostic's bets and every forecast-v5 fill (at least seven days before its scheduled end) go first. Gate 13's reference price under forecast-v5. Since 2026-10-06 each pass ends by moving the rows it wrote into `price_observations.jsonl.gz` (`compacted_bytes` in the result); readers see the same rows in the same order. Uncompressed, the store grew about 68 MB a day. A schema-faithful synthetic sample of 50,000 rows (345 bytes a row, as measured on the pilot) compressed 22× with full-precision prices and 44× with three-decimal ones; the pilot's first passes give the real ratio |
| Horizon diagnostic | Daily in `deploy/lean-pilot.json` since the decision (h = 1 h, 2026-10-05); off in code | Reads the stores and writes `diagnostics/horizon/<date>.json`, with a `funnel` showing where bought markets drop out before scoring. Only fills at least seven days before the market's scheduled end count: per post-entry horizon (1 h and 6 h), coverage with the reason for each missing reference, the share of references already at the outcome, and the CLV-win correlation. No gate counts (blueprint decision 6). The first eligible report wrote `decision.json`, which later reports never replace |
| Closing lines | Every 12 hours in `deploy/lean-pilot.json`; off in code | Backfill each traded market's 48-hour pre-close price window, at most 200 markets and 180 s per run, the horizon diagnostic's markets first. Kept as the comparison for the entry-price horizon diagnostic |
| Market metadata | 600 conditions, 48 Gamma requests per collection | Raised from 100 and 8 on 2026-10-02, when 4.5% of bought markets were in the store |
| Wallet batch | 20 | Oldest-polled first within the shallow cohort plus seeds never polled, which go first |
| Position snapshots | Latest 2 per wallet | Older snapshots are dropped after each collection; the exit-signal watermark is kept |
| Cohort v1 (Stage 3) | Daily in `deploy/lean-pilot.json`, freeze time unset; off in code | `cohort_v1.py` stage. Until `cohort_v1_freeze_at`, it rebuilds a provisional member list (T2, its matched comparison set, and the CLV-only ablation's wallets) from the current forecast-v5 generation. Collection polls those members first in every run (they count toward the wallet batch, 28 since 2026-10-07 so that about 12 places a run stay in the rotation). At the first cycle after the freeze time, it writes `cohort-v1/frozen-config.json` once, from the newest score generation that started at or before the freeze time (a later one would carry post-cutoff data), with the window from the power count. It logs the whole config and its hash, to be committed and recorded in the blueprint. Frozen members are never excluded by the cohort stage. Before the freeze, a member the cohort stage excludes is no longer polled, and it leaves the list at the next rebuild. Excluded overrides any tier (fixed 2026-10-07: an excluded member had been polled again, its whole history refetched and purged again). The owner sets the freeze time (plan S7) |
| Cohort v1 evaluation (Stage 3 step 6) | Once, from the frozen `cohort_v1` stage | `cohort_v1_eval.py`. At the first frozen-stage run at least 24 h after the window's evaluation date, it computes the pre-registered result: T2's event-weighted net 1 h improvement against zero (event-cluster bootstrap), the secondaries and the ablations. It writes `cohort-v1/result.json` once and logs it in parts. It refuses to run before the evaluation date |
| Cohort v1 signal capture (Stage 3 step 2) | Whenever a member list exists | `cohort_capture.py`, inside collection and before compaction. Each member's BUY fills of one market and outcome, first seen by this collection and at most 3 h old, become one signal. For each signal it reads the CLOB book (a $100 clip walked through the asks) and the taker fee (`/clob-markets` `fd`, cross-checked against Gamma), and appends a row to `cohort-v1/signals.jsonl` with the cohort ID and config hash. A signal without a measured cost is written as excluded, with its reasons. The collection result carries counts only (`cohort_v1_capture`). A capture failure is reported, never raised. Field names are not yet verified against live responses |
| Cohort v1 signal prices (Stage 3 step 3) | With the entry-price stage (every 6 h) | `cohort_prices.py` runs first in that stage, capped at 60 s. For each signal with a token, it fetches the bought token's `/prices-history` from 1 h before detection to 6 h after, at 5-minute fidelity. A window is fetched once it has ended and an hour has passed. Rows go to `cohort-v1/price_observations.jsonl` and receipts to `cohort-v1/price_receipts.jsonl`. A final window is never fetched again, and a failed one is retried after an hour. The result (`cohort_v1_prices`) carries counts and the median point spacing. A failure is reported, never raised, and the backfill still runs |
| Activity archive | Compacted at 32 MiB in code (`ACTIVITY_COMPACT_MIN_BYTES`) | Since Stage 3 step 0, a collection that leaves `polymarket_activity.jsonl` at 32 MiB or more moves its rows into a gzip segment under `polymarket_activity.jsonl.archive/` (`jsonl_archive.py`). Every reader goes through `iter_lines`, so rows, order and scores are unchanged; a test rescoring forecast-v5 before and after confirms it. A duplicated activity row is a fill counted twice, so compaction is exactly-once under a crash at any step (rename to a pending file, then a segment whose rename is the commit). The cohort purge also drops excluded wallets' rows from segments. Estimated 4× smaller from a pessimistic sample; the collection result's `activity_compaction` and `storage_mb` give the real figure |
| Watchlist cap | 64 wallets | The Railway Hobby volume is capped at 5 GB and every wallet keeps its activity history, so seeding stops adding wallets at the cap (`seeds_over_cap` counts the ones skipped). Nobody is removed. Raise it only with `storage_mb` evidence |
| Leaderboard seed | 100, month/profit in `deploy/lean-pilot.json` (`leaderboard_window`, `leaderboard_metric`; day/volume in code) | Bounded discovery input from the data API's `/v1/leaderboard`. Profit since 2026-10-03, after 63% of monthly-volume seeds were automated; a profit-seeded cohort is selected on recent winning, so only prospective results count (blueprint decision 6). Read 100 deep since 2026-10-04: at 25, every listed wallet was soon watched or excluded and freed slots stopped refilling (the watchlist fell from 64 to about 35). A rejected request appears in the collection result's `warning`. Excluded wallets are never re-seeded |
| Gate-13 power diagnostic | Daily in `deploy/lean-pilot.json`; off in code | Plan step 3 (`gate13_power.py`): scores forecast-v4 and forecast-v5 into a scratch directory (deleted afterwards), and writes `diagnostics/gate13/<date>.json` with per-wallet v5 CLV power and every excluded fill by reason (an unpriced hour split into `not_ended`, `fetch_failed` and `not_fetched`). The log line carries gate-13 and tailable counts under both versions, including the wallets the sample minimum alone blocks. Changes no score or threshold |
| Score version | `forecast-v5` in `deploy/lean-pilot.json` since 2026-10-05; `forecast-v4` in code | `forecast-v5` measures gate 13's CLV 1 h after each buy from the entry-price store (`post_entry_clv.py`, `docs/gate13-clv-v5-plan.md`). The switch changed no counts: 2 gate-13 passes, 0 tailable (`docs/benchmarks/2026-10-05-gate13-power.md`) |
| Cohort | Daily in `deploy/lean-pilot.json`, and in every cycle that scores or follows a score it has not acted on; off in code | Wallets the scorer labels `systematic` go to `excluded_wallets.txt` for good and their rows are deleted from every per-wallet store (activity dedupe index rebuilt); freed watchlist slots refill from the leaderboard |
| Activity budget | 10 calls/wallet | Shared across recent, historical, and boundary pagination |
| Cycle deadline | 20 minutes | Collection plus scoring together, including subprocess startup |
| Daily runtime allowance | 90 minutes in `deploy/lean-pilot.json` since 2026-10-06; 60 in code | Local UTC-day accounting, with a full cycle reserved before work. At 60 minutes the last 4–6 hourly runs of every day from 2026-10-03 to 2026-10-05 (18:00–23:59 UTC) exited as `budget_exhausted`: a day's work used about 2,600 s by 18:00, leaving less than the 1,200 s reservation. At the measured 0.34 GB peak, the extra 30 minutes costs an estimated under $0.50/month at the rates below |
| Disk guard | 512 MiB free | No cycle starts below this; the run exits nonzero as `disk_low` so Railway marks it failed. Every plan reports `disk_free_mb`, and each collection reports `storage_mb` per store |
| Worker RSS guard | 4,096 MiB | Sampled process-tree memory; separate container limit still needed |
| Worker log guard | 8 MiB/run | Stop a noisy child before its log grows indefinitely |

Activity budgets count client method calls, excluding the client's internal HTTP
retries. [Targeted metadata backfill](metadata-backfill.md) adds a ceiling of 100
conditions/eight actual HTTP attempts, durable cooldowns, and partial-work receipts.
Positions, economics, generic catalog pagination, and other endpoints are bounded
by the whole-cycle deadline. Two concurrent wallets and the existing 2-RPS
pacing setting reduce pressure; that setting is not a hard per-request RPS cap.
Historical coverage remains incomplete when a cap is reached. Recent checkpoint
and timestamp-boundary handling retain unfinished work for later collection.

The worker owns an OS file lock for its entire cycle, preventing another pilot
worker from overlapping even if the supervisor exits. A child-side deadline
provides a second timeout if the supervisor dies. The supervisor records wall
time, peak sampled RSS, memory-time, and observed CPU time and kills an
over-budget child process tree. These samples are diagnostic estimates, not a
Railway invoice or a container-enforced memory guarantee.

Before work begins, the worker reserves the maximum cycle runtime on disk. A
normal exit charges elapsed work and refunds unused time. A killed worker keeps
the full reservation charged, so repeated restarts cannot erase its cost. A
possible UTC-midnight crossing reserves in both days; completed crossings also
charge elapsed time to both days conservatively. A run needs room for the whole
reservation even if only collection is due. Failed stages wait their normal
attempt interval before becoming due again. Busy and not-due jobs exit promptly.

Scoring writes wallet enrichment and wallet bets into a new directory. It checks
record fields, counts, JSON numbers, file hashes, and input size/mtime/inode
stability, then writes a manifest and swaps `current.json` last. The manifest
records UTC timestamps, the package source hash, score versions, and missing
context files. Readers must follow the pointer, never directory recency.
Same-stat input tampering is outside this check; exclusive ownership is required.

## Running a reviewable local pilot

Install from the repository root using Python 3.12+:

```powershell
python -m pip install -e "services/polymarket-ingestor[pilot]"
python -m marketsignalos_polymarket.lean_pilot --data-dir ./services/ingestor/data/pilot --config ./deploy/lean-pilot.json --plan
```

`--plan` is the default and reads state without creating files or calling an
upstream API. An explicit `--run` performs collection against public endpoints:

```powershell
python -m marketsignalos_polymarket.lean_pilot --data-dir ./services/ingestor/data/pilot --config ./deploy/lean-pilot.json --run
```

Use a dedicated pilot data directory. The historical multi-gigabyte activity
dedupe index still loads into RAM during collection and is not made smaller by
batching wallets. Do not point the pilot at that archive and assume the 4-GiB
guard makes collection viable. Scoring still uses the JSONL sharder; the earlier
Parquet parity experiment is not yet the production scoring path.

Keep `DATABASE_URL` unset for this pilot: the worker rejects dual-write mode.
Its explicit data directory also determines its watchlist path. Do not run the
legacy API scheduler, manual ingest buttons, deep pipeline, or another writer
against this same directory; their locks are not coordinated with this worker.
No live scheduler was enabled by adding this command.

Inspect `.lean-pilot/state.json` and `.lean-pilot/runs/<run-id>/` for the job,
receipt, result, resource samples, and operator log. A `partial` collection means
some wallet hydration or leaderboard work did not complete; it does not advance
the successful-collection timestamp. Scoring can still complete with untrusted
wallets excluded by the existing model gates. A successful scoring job describes
computation completion, not fresh inputs, complete market coverage, qualified
wallets, or verified predictive performance.

An interrupted or exceptional collection sets `recovery_required` on the next
inspection by a worker, preserves its runtime charge, and exits nonzero. This
is deliberate: the legacy JSONL append/index/checkpoint writes are not one
transaction. Interrupted scoring retains prior published scores and leaves its
unused generation for inspection.

**Recovery is manual, by design (blueprint §6 Stage 2), but needs no shell.**

1. The worker's log line reports `recovery_required` and names the interrupted run
   as `recovery_run_id`.
2. Set the Railway service variable `PILOT_RECOVER` to that id. The next scheduled run
   repairs the data directory before collecting (`pilot_recovery.py`, `--recover RUN_ID`).
   It:
   - cuts any torn final row from each append-only JSONL store, keeping the cut bytes;
   - counts invalid lines elsewhere without removing them;
   - rebuilds an unreadable wallet-checkpoint file from the stored activity (never
     newer than the lost one, so trades are refetched, not skipped);
   - rebuilds the activity dedupe index from the stored rows.
3. The flag is cleared only if every step succeeded. Either way the receipt is
   `.lean-pilot/recoveries/<run id>.json`, and the cut bytes sit beside it.
4. Remove the variable afterwards.

A different run id is refused, so a variable left set never repairs a later incident
on its own. A state written before run ids were recorded takes the id `unrecorded`.
Checkpoint and watchlist files are now replaced atomically, so new incidents can
leave only torn JSONL rows and a stale index.

## Budget envelope, not a cost guarantee

Reserve a planning allowance of $4 for the worker, $7 for public serving, $2 for
storage/transfer, and $2 contingency. These are allocation targets, not measured
service prices. Railway's Hobby minimum is $5, credited toward resource usage;
RAM is $10/GB-month, CPU $20/vCPU-month, volumes $0.15/GB-month and service egress
$0.05/GB. Verified 2026-09-09 in [Railway pricing](https://docs.railway.com/pricing).

As an illustration, a worker averaging 4 GB RAM and one vCPU for one hour each
day uses about $2.50/month of compute at those rates on a 30-day normalization.
Actual Python memory, CPU, deployment startup, guard overshoot, supervisor
overhead, storage and traffic change this result. The local allowance covers
time spent inside charged cycles, not all billed container life. Frequent
unnecessary invocations still incur startup costs. Serving memory must be
measured independently; the current API and Next.js app are not proven to fit
their combined $7 allowance.

The raw archive, score generations, logs, and abandoned generations currently
have no automated retention policy. At the earlier measured size, each complete
score generation adds approximately 426 MB. Retention and backup/restore are
required before leaving this running unattended for weeks.

Railway scheduled workers must exit; an existing active execution causes the
next scheduled run to be skipped, and Railway does not automatically terminate
it. The candidate deployment is one hourly scheduled worker with its own volume,
plus separately served published data. Do not use the current combined API
service as that scheduled worker. See [Railway cron jobs](https://docs.railway.com/cron-jobs).

Before paid provisioning, review the concrete service layout, measured pilot
resource use, and a proposed early alert at $10 with a $15 workspace compute
limit. Railway's hard limit can take all workloads in that workspace offline;
Agent usage has a separate limit. Nothing here configures either limit. See
[Railway cost controls](https://docs.railway.com/pricing/cost-control).
*2026-10-07:* the owner set both, an alert at $10 and a compute limit at $15
(owner-reported). Projected usage is about $2 a month (blueprint §6 Stage 2 note).

## Built so far and what remains

Earlier passes established the public dashboard/API, wallet dossiers, scoring
and paper ledger, and prepared observability configuration. Offline storage
work converted 16.9 million historical observations into a 7.39-times smaller
Parquet dataset and verified full score-output parity. The measured scorer still
needed roughly 3.1 GiB; faster aggregate queries did not eliminate that cost.
See the [storage benchmark](storage-benchmark.md) and
[full scoring comparison](enrichment-shadow.md) for scope and evidence.

This pass adds collection-only operation, rotating batches, activity-call limits,
persisted stage cadence and runtime reservations, resource receipts, overlap and
deadline guards, and validated local score publication. It does not provision
Railway or update the live website. The integration branch also includes the
earlier platform preparation: private operator endpoints, public freshness
status, restart receipts, website refresh, and API/web deployment templates.
Merge fixes preserve the newer metrics/fast-lane support, forward watchlist
request bodies and caller credentials, hide operator controls publicly, and
configure authenticated Alloy scraping. These components still need live
deployment, scrape, and alert-delivery verification.

Next passes, in order:

1. Make raw collection restart-safe and memory-bounded: replace the in-memory
   dedupe index with transactional storage; prove checkpoint recovery and define
   retention. An offline fixture now exercises real scoring through the pilot;
   next run a bounded fresh-data pilot with quality and resource reports.
2. Publish complete serving snapshots including positions, markets, coverage,
   and separate source/score timestamps. Switch API reads to a pinned generation
   with parity tests, rollback, and explicit stale states. The earlier operator
   authentication and deployment preparation are now integrated on the feature
   branch; the public API still reads the legacy JSONL serving files.
3. Exchange snapshots through cloud storage, add retention and restore tests,
   then prepare/review the Railway worker and lightweight serving deployment.
   Activate billing controls and delivery-tested operational alerts only with
   the concrete deployment and workspace scope established.
4. Improve market metadata coverage and run the prospective paper-following
   protocol: freeze wallet selection, use executable follower prices with delay,
   costs and slippage, then evaluate later outcomes. Do not loosen eligibility
   gates to populate an empty feed. ML comes after trustworthy point-in-time data.

Every development pass should report what was built, what was verified, what
remains, and whether any cloud cost was actually incurred. Infrastructure fit
and forecasting evidence are separate acceptance criteria; the monthly budget
does not assume profits from following trades.

## Verification for this integrated pass

- 534 backend/ingestor tests passed; one existing production-RSS test was
  skipped because its RSS source is unavailable on Windows.
- Ruff passed for both Python packages; strict mypy passed for the API and
  ingestor; frontend ESLint and the Next.js production build passed.
- New tests cover independent cadence, partial/failed success timestamps,
  interrupted-run reservations, midnight accounting, overlap locks, subprocess
  timeout/RSS limits, the orphan-worker deadline, snapshot publication failures,
  and real scoring through a small offline pilot fixture.
- The existing timing test now checks requested limiter spacing with a
  controlled clock instead of failing at a Windows wall-clock tick boundary.
- No live collection, Railway provisioning, billing-limit change, or live
  Grafana scrape/notification was performed. The subsequent
  [Stage 0 pass](gate-attrition.md) completed Alloy bearer-token binary validation,
  root type-check repairs, and qualification attrition analysis. Live Grafana
  scrape/notification checks and Railway activation remain outstanding.
