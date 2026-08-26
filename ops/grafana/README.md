# Shipping metrics to Grafana Cloud

`/metrics` exports real collectors and `ops/prometheus/alerts.yml` holds 13
rules — but until something scrapes the deployment, not one of those alerts can
fire. This directory is what closes that gap.

> **What this changes.** Before: instrumentation committed to a repo. After: a
> monitored service with alert history. Those are different claims, and only
> the second one survives the question "how would you have known?"

---

## Architecture

```
Railway: API  ──/metrics──>  Railway: Grafana Alloy  ──remote_write──>  Grafana Cloud
   (exports)                    (scrapes + pushes)                    (stores, alerts, graphs)
```

Grafana Cloud's hosted Prometheus is **push-based** — it does not reach out and
scrape your endpoint. Something has to pull `/metrics` and `remote_write` it,
and that something is Grafana Alloy. It runs as its own tiny service, so
nothing has to live on your laptop.

Alloy replaced Grafana Agent, which reached end-of-life on 2025-11-01. Their
config formats are **not** interchangeable — any tutorial showing
`metrics_config:` YAML is for the dead product.

---

## What's already done

| File | Purpose |
|---|---|
| `config.alloy` | Scrape config, fully parameterized by env vars |
| `dashboard.json` | 20-panel dashboard, importable as-is |
| `../prometheus/alerts.yml` | 13 alert rules (7 incident regressions + 6 SLOs) |

CI enforces that every metric named in the dashboard and in the alert rules is
actually exported (`apps/api/tests/test_grafana_dashboard.py`,
`apps/api/tests/test_alert_rules.py`), so a renamed collector fails the build
rather than silently producing an empty panel or a rule that never fires.

## What you have to do

Five steps, roughly 30 minutes.

### 1. Create the Grafana Cloud account

<https://grafana.com/auth/sign-up/create-user> — free tier, no credit card.

You get 10,000 active metric series, 14-day retention, and 3 users.

**You will use well under the series limit.** Rough arithmetic on this app's
label dimensions: request metrics ~220 series (routes × methods × statuses, plus
histogram buckets), upstream ~300 (hosts × endpoints × outcomes), pipeline ~170
(stages × buckets), model-health gauges ~10, feed ~40, plus ~30 default Python
process metrics. Call it **1–2k series** in steady state, so there is a lot of
headroom before cost is a question.

### 2. Collect three credentials

From your stack's **Details → Prometheus** page:

| Value | Looks like | Goes into |
|---|---|---|
| Remote write endpoint | `https://prometheus-prod-XX-<region>.grafana.net/api/prom/push` | `GRAFANA_CLOUD_PROM_URL` |
| Username / instance ID | a number, e.g. `1234567` | `GRAFANA_CLOUD_PROM_USER` |
| Access policy token | `glc_...` | `GRAFANA_CLOUD_API_KEY` |

Create the token under **Access Policies** with the `metrics:write` scope. Use
an access policy token, not a legacy API key.

### 3. Deploy Alloy as a second Railway service

New service in the same project, from the Docker image `grafana/alloy:latest`.

Set these variables on the **Alloy** service:

```
MSOS_METRICS_TARGET      = <your-api>.up.railway.app:443
GRAFANA_CLOUD_PROM_URL   = <from step 2>
GRAFANA_CLOUD_PROM_USER  = <from step 2>
GRAFANA_CLOUD_API_KEY    = <from step 2>
```

Start command:

```
run /etc/alloy/config.alloy --server.http.listen-addr=0.0.0.0:12345 --storage.path=/var/lib/alloy/data
```

`config.alloy` needs to reach the container. Simplest path is a one-line
Dockerfile in this directory that layers it onto the base image:

```dockerfile
FROM grafana/alloy:latest
COPY ops/grafana/config.alloy /etc/alloy/config.alloy
```

Point the Railway service at that Dockerfile with the repo root as build
context.

> **Note the port.** `MSOS_METRICS_TARGET` is `host:port` with **no scheme** —
> the scheme is set separately in `config.alloy`. Railway serves public domains
> on 443, so the `:443` suffix is required and its absence is the most common
> reason the target shows as down.

### 4. Import the dashboard

Grafana Cloud → **Dashboards → New → Import → Upload JSON file** →
`ops/grafana/dashboard.json`. Pick your Prometheus data source when prompted.

### 5. Load the alert rules

`alerts.yml` is standard Prometheus rule format, so `mimirtool` uploads it
directly:

```bash
mimirtool rules load ops/prometheus/alerts.yml \
  --address="https://prometheus-prod-XX-<region>.grafana.net" \
  --id="$GRAFANA_CLOUD_PROM_USER" \
  --key="$GRAFANA_CLOUD_API_KEY"
```

Confirm the exact `--address` on your stack's Details page — the host differs
per region and is *not* the same string as the `/api/prom/push` write endpoint.

To manage them as Grafana-managed rules instead (editable in the UI, and they
can route to notification policies), point mimirtool at
`<your-grafana-url>/api/convert/` with the tenant id set to `1`.

---

## Verifying it works

In order, because each step depends on the one before:

1. **Alloy is up** — its service logs should show no config parse errors.
2. **The target is up** — Alloy's own UI on port `12345`, under Targets, should
   list `marketsignalos-api` as UP. A DOWN target here is almost always the
   missing `:443`.
3. **Data is arriving** — in Grafana Explore, run `msos_pipeline_running`. It
   should return a value within about two minutes of Alloy starting.
4. **Alerts are loaded** — Alerting → Alert rules should list 13.
5. **Alerts evaluate** — they should all read Normal. `SignalsStale` may show
   as No Data until the first successful ingest creates the series, which is
   correct: the rule is deliberately written so it cannot fire on a deployment
   that has never run the pipeline.

A useful smoke test for the whole loop: stop the ingest scheduler and wait.
`SignalsStale` should fire at the 3h mark. That single test proves scrape,
storage, rule evaluation, and notification all work — which is worth more than
any amount of config review.

---

## Things worth knowing

- **Scrape interval is 60s** (1 data point per minute), Grafana Cloud's billing
  unit. Every alert tolerates this: the shortest `for` is 5m and the shortest
  rate window is 10m, so nothing depends on sub-minute resolution. Don't lower
  it without re-checking the rules.
- **14-day retention on the free tier.** Fine for alerting and for showing the
  system works; not enough for quarter-over-quarter trends.
- **Public dashboards** (externally shareable read-only links) exist in Grafana,
  but whether the free tier includes them was not verifiable from here — check
  Dashboard → Share → Public dashboard in your own stack before relying on it
  for anything you plan to link publicly.
- **The API has no auth on `/metrics`.** It's a public endpoint on a public
  Railway domain, so treat everything it exposes as public. Nothing there is
  sensitive today — counts, durations, and model parameters, no wallet
  addresses (labels are normalized to `:id` precisely to keep it that way) —
  but that's a property worth re-checking whenever a metric is added.
