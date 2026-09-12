# Lean pilot: $15 monthly design target

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
| Collection | Every hour | Refresh a rotating subset of eligible wallets |
| Scoring | Every 24 hours | Recompute derived scores into a new local generation |
| Wallet batch | 20 | Oldest-polled first within the existing shallow cohort |
| Leaderboard seed | 25, day/volume | Bounded discovery input; no profit leaderboard |
| Activity budget | 10 calls/wallet | Shared across recent, historical, and boundary pagination |
| Cycle deadline | 20 minutes | Collection plus scoring together, including subprocess startup |
| Daily runtime allowance | 60 minutes | Local UTC-day accounting, with a full cycle reserved before work |
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
transaction. Before clearing that flag, preserve the files, validate JSONL line
integrity, reconcile the activity index with durable rows, and check wallet
checkpoints/hydration against what was actually persisted. There is no automatic
repair command in this increment. Interrupted scoring retains prior published
scores and leaves its unused generation for inspection.

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
