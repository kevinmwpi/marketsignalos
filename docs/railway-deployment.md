# Railway deployment: lean-pilot worker

Railway runs one thing for this project: the lean-pilot worker, as an hourly cron
service with a volume. The website does not use Railway. The dashboard
(`apps/dashboard`) is a static site that reads the published research snapshot
from GitHub; `.github/workflows/pages.yml` builds it with `VITE_STATIC_SITE=1`, so
it makes no API requests, and publishes it to GitHub Pages.

## Scheduled worker (lean pilot)

One Railway service runs `marketsignalos_polymarket.lean_pilot` once an hour and
exits. The worker decides what is due (collection hourly, scoring daily) and keeps
its runtime allowance on its volume; see [lean-pilot.md](lean-pilot.md).

| Setting | Value |
|---|---|
| Source | This repository, root `/` |
| Config file | `/railway.toml` (Railway reads it from the repository root automatically) |
| Builder | Dockerfile, `deploy/worker.Dockerfile` |
| Schedule | `7 * * * *` (hourly at minute 7), restart policy `NEVER` |
| Volume | One volume, any mount path; Railway sets `RAILWAY_VOLUME_MOUNT_PATH` |
| Data directory | `<volume>/pilot` |
| Variables | None required. Do not set `DATABASE_URL`: the worker refuses to start. |

The image holds only production dependencies from `uv.lock` and the ingestor
source. `.github/workflows/worker-image.yml` builds it on every change to these
inputs and checks three things without network access:

- plan mode runs;
- the worker refuses to start without a data directory;
- the process supervisor works inside the image.

Local equivalent:

```bash
docker build -f deploy/worker.Dockerfile -t marketsignalos-worker .
docker run --rm --network none -e POLYMARKET_PILOT_DATA_DIR=/tmp/pilot marketsignalos-worker --plan
```

Without a data directory the entry point exits with status 2 rather than keep its
cadence and runtime accounting on a disk that is wiped every run.

**Not verified until provisioning:**

- that Railway accepts these config keys;
- that a cron service mounts its volume;
- the first run's cold-start time, peak memory and cost.

Record them in the Stage 2 evidence file (`docs/handoff-blueprint.md`).

**Provisioning steps, after budget approval:**

1. Create one Railway service from this repository. It picks up `/railway.toml`
   on its own. Before that file existed, Railway's default build ran
   `uvicorn main:app` against the repository root and crashed on start.
2. Remove any public domain under the service's networking settings. The worker
   serves no HTTP traffic.
3. Attach a volume before the first run: open the Command Palette (Ctrl/⌘+K) or
   right-click the project canvas, choose to create a volume, connect it to this
   service, and give it a mount path such as `/data`. The worker finds it through
   `RAILWAY_VOLUME_MOUNT_PATH`; without one it exits with "No data directory".
4. Set a usage alert at $10 and a workspace compute limit at $15. The hard limit
   takes every workload in the workspace offline.
5. Let the first scheduled run finish. Check `<volume>/pilot/.lean-pilot/runs/<id>/`
   for the receipt and resource report.

Stop the worker by removing the cron schedule. A run that is in progress finishes
within its own 20-minute deadline.

## Serving API (not deployed)

`apps/api` is not deployed anywhere; serving it publicly is blueprint Stage 5. When it
is, keep these properties, which the code already enforces:

- Public GET feeds and `/platform/status` need no credentials. Mutation routes,
  `/ingestor/status`, `/signals/notifications/status` and `/metrics` require
  `Authorization: Bearer <ADMIN_API_TOKEN>` (32+ characters). A missing token
  disables operator access; it never enables anonymous writes.
- `ALLOW_UNAUTHENTICATED_ADMIN=1` is a local-only bypass; production and Railway ignore it.
- JSONL storage allows one process and one replica (`WEB_CONCURRENCY=1`) with a
  persistent data directory. `API_READ_ONLY=1` serves reads with collection disabled.
- `deploy/railway-api.env.example` lists the variables; `scripts/start-api.sh`
  validates the configuration and starts uvicorn.

Never put an operator token in a `VITE_*` variable: those are compiled into the
public site.
