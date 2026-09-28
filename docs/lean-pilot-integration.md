# Lean pilot integration and merge decision

This pass reconciles `codex/lean-pilot-15-budget` at `1530fca` with the Replit
application on `main` at `079e23d`. The integration branch is
`codex/lean-pilot-replit-integration`. Use its pull request for the merge; a direct
merge of the old branch has directory-migration and tooling conflicts.

## Current layout

| Purpose | Current location |
| --- | --- |
| Published React/Vite dashboard | `artifacts/marketsignalos-dashboard` |
| Replit API launcher and mounted adapter | `artifacts/api-server` |
| Active Python API package | `.migration-backup/apps/api` |
| Active Python ingestion/research package | `.migration-backup/services/polymarket-ingestor` |
| Frozen diagnostic evidence and newer runbooks | `docs` |
| Pilot configuration | `deploy/lean-pilot.json` |
| Active checks and scheduled collection | `.github/workflows` |

Despite its name, `.migration-backup` contains the backend imported by the live
adapter. Keep it in the deployment. Its Next.js app, Railway templates and old
workflow are historical copies, not the deployed frontend or an active scheduler.
Earlier documents use paths from the pre-Replit layout. For Python commands,
use the package locations above or install through `uv sync --frozen` from the
repository root. The legacy deployment templates need an explicit build context
review before any separate Railway deployment; merging code does not activate them.

## What this integration preserves and adds

- Preserves the Replit routes, Python startup commands, Vite dashboard, frozen
  production dependency versions and independent six-hour GitHub collector.
- Integrates bounded pilot execution, resource receipts, locking, atomic score
  publication, metadata backfill budgets and provenance, coverage repair,
  Parquet/shadow tools, and the frozen Stage 0/1 diagnostic evidence.
- Resolves added backend files into the locations actually imported by Replit.
- Adds explicit `API_READ_ONLY=1` to Replit production. Private/operator routes
  are disabled even with credentials; startup rejects background collection.
  This permits public serving without an ingestion token or persistent volume.
  A separate operator deployment retains strict token, storage and single-worker
  validation. JSONL read caches can still be written locally in public-serving mode.
- Handles authentication paths when the backend is mounted under `/api`.
- Restores active CI for the migrated Python packages, adapter, collector and
  Vite dashboard. Research/development dependencies are locked in the dev group;
  production continues to use `uv sync --frozen --no-dev`.

The worker remains opt-in. No paid resources, billing settings, extra production
ingestion jobs, or automatic trading are enabled. The live scored API still reads
legacy JSONL files, not the pilot's versioned score generations.

## Merge gate

Code integration is appropriate to merge when all three checks on the integration
PR pass: Python on Linux, Python on Windows, and the dashboard. They exercise the
pilot's budget/locking/publication invariants, scorer and metadata tests, public
and protected API routes, exact production build/start commands in an isolated
`.pythonlibs` environment, schema/fallback loader tests and frontend build.

Local verification on September 28: 645 Python tests passed, one failed on a
migrated fixture path, and one platform-specific test skipped. The path was fixed
and both metadata probe tests then passed. Backend lint, strict cross-package
types (107 files), dashboard types and four loader tests passed. The PR checks
provide the complete final-tree rerun on both operating systems.

After merging, sync Replit and republish once, then check `/api/healthz`,
`/api/platform/status`, the feed routes and the research capture date. GitHub
snapshot updates continue independently and do not need recurring deployments.
For rollback, revert the integration merge or restore the previous Replit
publication. Do not delete or rewrite collected data to roll back application code.

## Live collection evidence, September 28

The 30 scheduled runs returned by GitHub from September 20 through September 28
included 24 successful publications and six failures. The separate initial
manual run succeeded. These are observed runs, not proof every expected interval
was dispatched on time. GitHub scheduling is best effort.

The [September 28 run](https://github.com/kevinmwpi/marketsignalos/actions/runs/36431706719)
published a complete capture at 13:52 UTC, visible on the public Replit page with
10 wallets. Public health returned 200. The page still correctly shows an empty
qualified feed. The [September 26 failure](https://github.com/kevinmwpi/marketsignalos/actions/runs/36255225054)
contained upstream 429 responses; automatic publication rejected the partial
capture. Other failures require per-run diagnosis before assigning a cause.

## What remains after code integration

1. Add bounded collection failure diagnostics and measure schedule delays, coverage
   and stale intervals. Keep publication gates intact when requests are throttled.
2. Make full collection restart-safe and memory-bounded, with durable storage,
   retention and restore evidence. Measure actual worker usage before any paid
   provisioning against the $15/month target.
3. Publish complete versioned serving datasets and validate API parity before
   switching away from the legacy stores. Preserve source versus score timestamps.
4. Resolve metadata/settlement semantics and qualify wallets with complete inputs.
   Run prospective paper-following with frozen selection, delay and costs before
   interpreting historical results as an investable edge. ML follows that evidence.

Merge readiness is a code/deployment compatibility decision. It is not proof of
an affordable full-history service, statistical skill, or profitable following.
