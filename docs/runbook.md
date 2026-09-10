# Runbook

Deployment settings, initial collection, authentication, restart verification,
backups and rollback are in [railway-deployment.md](railway-deployment.md).

## First checks

1. Read `/health` for process liveness, then `/platform/status` for data recency.
   A healthy process can serve stale or missing data. Freshness is based on a
   durable successful-run receipt and required dataset modification times; it
   does not certify upstream or per-wallet completeness.
2. With an operator bearer token, inspect `/ingestor/status`: running state,
   progress, latest summary, warning, errors and scheduler skipped ticks.
3. Review coverage and quarantine counts in `/signals/skilled-bets/summary`.
   An empty feed is not evidence of a failed deployment if no wallets qualify.

## Failure handling

| Symptom | Action |
|---|---|
| Startup rejects configuration | Check the token, volume mount and data directory; retain one API worker and replica |
| Operator endpoint returns 401/503 | Supply the correct bearer token / configure operator access; do not enable anonymous production writes |
| Data unavailable on a fresh volume | Run an authorized initial deep ingest and inspect hydration counts |
| Stale data with healthy API | Inspect scheduler, skipped runs, last error, upstream timeouts and volume capacity |
| Partial run | Inspect discovery warning and incomplete wallets; do not label it full coverage |
| API restarted during a run | The receipt becomes failed; the next scheduled pass retries through existing wallet checkpoints |
| Memory grows or API slows | Reduce concurrency and batch sizes, measure file growth; migrate to indexed database reads before horizontal scaling |
| Upstream schema/contract changes | Reconcile a small sample against current official sources before expanding ingestion |

Pause collection with `INGEST_EVERY_MINUTES=0` and redeploy after active work
finishes. Preserve all data files, checkpoints and sidecar indexes in a consistent
backup. Never delete them to clear an application error without understanding
the lost history and replay implications.
