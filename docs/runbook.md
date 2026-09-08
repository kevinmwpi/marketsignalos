# Runbook

This is the operational entry point. Use the maintained
[observability guide and per-alert runbooks](observability.md) for all 13 rules,
and [Grafana Cloud delivery](../ops/grafana/README.md) to provision the existing
Alloy scrape and remote-write path. The local development commands are in
[README.md](../README.md).

## Establish monitoring before claiming it is live

1. Verify the API's `/health` and `/metrics`. A healthy process alone does not
   establish fresh signals or successful ingestion.
2. Verify the Alloy target and remote-write credentials. `MSOS_METRICS_TARGET`
   is `host:port`, without a scheme; the current config uses HTTPS.
3. In Grafana, confirm `up{job="marketsignalos-api"}` and actual pipeline/feed
   series advance over several scrapes. Confirm the 13 rules are loaded and
   evaluating without errors.
4. Configure a contact point and route matching the rule labels. A contact-point
   test checks delivery only. Record an intentional test rule firing and
   recovering through the same notification route, preferably in staging.
5. In a planned staging drill, stop ingestion long enough to exercise the
   existing freshness rule, then resume and verify recovery. Record timestamps
   for the input change, scrape, firing, receipt and recovery. Do not disable
   production ingestion merely to make a dashboard turn red.

Grafana documents [contact-point tests](https://grafana.com/docs/grafana-cloud/alerting-and-irm/alerting/alerting-rules/manage-contact-points/)
and [notification routing](https://grafana.com/docs/grafana-cloud/observe-and-act/alert-and-measure-reliability/alerting/configure-notifications/create-notification-policy/).
Account credentials belong in the hosting provider's secret configuration,
never in a committed runbook or benchmark report.

## Incident triage

| Symptom | First evidence to inspect |
|---|---|
| No recent signals | Last successful run, feed freshness, wallet coverage and model-health panels |
| Ingestion stalled | Running flag, progress heartbeat, per-stage duration and RSS |
| Upstream slowdown | Request construction versus transport latency, cookie-jar size, retries and rate-limiter wait |
| Scores become uniformly extreme | Resolved-event count, prior parameters, saturation ratio and score version |
| Slow feed/API | RED metrics, cache hit rates, active ingestion stage and memory pressure |
| Healthy local endpoint but no alerts | Alloy scrape target, remote-write errors, rule loading, notification route |

Follow the corresponding incident-specific actions in `docs/observability.md`.
Keep a dated record of the trigger, detection, mitigation, source data and
verification after recovery. Preserve raw history and checkpoints during fixes.

## Storage experiments

Run the [offline storage benchmark](storage-benchmark.md) on a stable source
snapshot. It does not migrate production or remove the global deduplication
index. Require output parity, resource limits, restart safety and a restore test
before changing the production read/write path.
