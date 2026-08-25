# Observability

MarketSignalOS exports Prometheus metrics at `GET /metrics` and ships alerting
rules in [`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml).

The design constraint that shaped everything here: **this system's failures are
silent.** Four production incidents are recorded in
[`fix-lessons-learned.md`](fix-lessons-learned.md), and not one of them
announced itself. There were no exceptions, no 5xx spike, no crash loop, and in
every case the full test suite stayed green. Two ran for weeks. One ran for
13.5 hours inside a single process that looked healthy the entire time.

So the metrics below are not a generic dashboard. Each one is the specific
quantity whose wrongness went unnoticed, and each alert is the check that would
have caught it.

---

## Incident → alert coverage

| Incident | What went wrong | Undetected for | Alert that now catches it | Detection latency |
|---|---|---|---|---|
| [2026-07-07](fix-lessons-learned.md) | Settlement predicate required a Gamma flag state that never occurs; every enrichment computed `resolved_bets=0` | ~6 weeks | `EnrichmentProducesNoResolvedBets` | 15 min |
| [2026-07-10](fix-lessons-learned.md) | Unbounded httpx cookie jar made request *construction* O(n²); deep runs ground to a halt | Multiple runs | `UpstreamRequestBuildSlow`, `HttpClientCookieJarGrowing` | 10 min |
| [2026-07-13](fix-lessons-learned.md) | Whale hydration accumulated whole activity histories in RAM; 18 GB on a 15.7 GB box, swap-thrashing | 13.5 h | `PipelineStalled`, `PipelineMemoryHighWater` | 35 min |
| [2026-07-14](fix-lessons-learned.md) | Undamped Newton diverged, poisoning the empirical-Bayes prior; `skill_likelihood` saturated to 1.0 for 100% of wallets | Until manual audit | `PopulationPriorOutOfDomain`, `SkillScoreNotDiscriminating` | 5–15 min |

Three of these four quantities were **already being logged every run**. A log
line nobody greps is not a monitor — the gap was never data collection, it was
evaluation.

---

## Metric inventory

Collectors register on the prometheus_client default registry from two modules,
split along the package seam:

- `marketsignalos_polymarket.metrics` — pipeline, upstream client, model health
- `marketsignalos_api.observability.metrics` — HTTP requests, feed serving

The API registers the pipeline collectors at boot (`register_pipeline_metrics`)
so every series exists from process start. This matters more than it sounds: a
series that first appears mid-scrape-window makes `rate()` wrong, and makes
`absent()` alerts fire on every deploy.

### Pipeline

| Metric | Type | Purpose |
|---|---|---|
| `msos_pipeline_runs_total{kind,outcome}` | counter | Run outcomes by shallow/deep |
| `msos_pipeline_stage_duration_seconds{stage}` | histogram | Per-stage wall clock |
| `msos_pipeline_stage_rss_bytes{stage}` | gauge | RSS sampled at each stage end |
| `msos_pipeline_last_success_timestamp_seconds{kind}` | gauge | Freshness SLO input |
| `msos_pipeline_progress_timestamp_seconds` | gauge | **Stall detector** — see below |
| `msos_pipeline_running` | gauge | 1 while a run is in flight |
| `msos_pipeline_records_written_total{store}` | counter | Durable write throughput |

**On the progress gauge.** A wallet backfill can legitimately occupy one stage
for hours, so stage transitions alone cannot separate "slow" from "wedged".
Liveness has to be asserted positively by the work itself, so every stage
transition *and every persisted activity page* stamps this gauge. During the
2026-07-13 stall it would have stopped advancing while the process, its logs,
and its CPU usage all still looked plausible.

### Upstream HTTP

| Metric | Type | Purpose |
|---|---|---|
| `msos_upstream_requests_total{host,endpoint,outcome}` | counter | Request outcomes |
| `msos_upstream_request_duration_seconds{host,endpoint}` | histogram | Transport latency |
| `msos_upstream_request_build_seconds{host}` | histogram | **Construction latency only** |
| `msos_upstream_retries_total{host,endpoint,reason}` | counter | Retry pressure |
| `msos_http_client_cookie_jar_size` | gauge | Cookie accumulation |
| `msos_rate_limiter_wait_seconds` | histogram | Time queued behind the shared budget |

**On splitting build from transport.** `PolymarketClient` calls
`build_request()` and `send()` separately rather than `client.get()`, purely so
these two can be timed apart. The 2026-07-10 failure lived entirely in
construction while sockets stayed fast; a single round-trip timer averages the
two together and reports only "requests got slow" — the reading that sent the
original investigation through three wrong theories (machine sleep, thread
deadlock, dead sockets) before a `py-spy` dump found the real answer. Timed
apart, the same failure is a one-glance read on which half regressed.

`endpoint` labels are normalized (`/activity`, `/positions`, `:id` for wallet
addresses and market ids) so per-wallet cardinality cannot reach the registry.

### Model health

| Metric | Type | Purpose |
|---|---|---|
| `msos_enrichment_resolved_bets` | gauge | Settled bets available to the fit |
| `msos_enrichment_wallets` | gauge | Wallets scored |
| `msos_enrichment_markets` | gauge | Markets in the index |
| `msos_enrichment_tailable_wallets` | gauge | Wallets clearing every gate |
| `msos_enrichment_duration_seconds` | histogram | Two-pass fit duration |
| `msos_population_prior_mu` | gauge | Fitted hyperprior mean (log-odds) |
| `msos_population_prior_sigma2` | gauge | Fitted hyperprior variance |
| `msos_skill_likelihood_saturated_ratio` | gauge | Fraction of wallets at `>0.999` |

These are the "is the science still working" series. They answer a question no
process metric can: the pipeline can be perfectly healthy — every request 200,
every stage on time, memory flat — while the numbers it produces are garbage.

### API and feed

| Metric | Type | Purpose |
|---|---|---|
| `msos_http_requests_total{method,path,status}` | counter | RED: rate + errors |
| `msos_http_request_duration_seconds{method,path}` | histogram | RED: duration |
| `msos_feed_compute_duration_seconds{source}` | histogram | Feed latency by serving layer |
| `msos_feed_cache_events_total{layer,result}` | counter | Memo vs disk-spill hit rates |
| `msos_feed_signals` | gauge | Signals in the latest feed |

`path` is always the **route template** (`/signals/wallets/{proxy_wallet}`),
never the raw path — a wallet address in a label would mint one series per
address. Unmatched paths collapse to `<unmatched>` so a 404 scan cannot create
series at will.

`source` distinguishes `memory` (lru_cache), `disk` (the spill artifact), and
`compute` (a full walk of the activity file). Only the last can take >900 s, so
the split is what makes a latency regression diagnosable rather than merely
visible.

---

## Alert reference

### `EnrichmentProducesNoResolvedBets`

`msos_enrichment_resolved_bets == 0 and msos_enrichment_wallets > 0` for 15m.

The skill model has no settled outcomes to fit against. `skill_likelihood`
carries no information and the feed is empty or arbitrary.

The `wallets > 0` conjunct proves enrichment actually ran. Without it the rule
fires on every fresh deploy — and an alert that cries wolf at boot is one
someone silences before it ever catches the real thing.

**First check:** run a `Counter()` over the real `polymarket_markets.jsonl` for
the flag combination the settlement predicate requires, and confirm that
combination actually occurs upstream. That single check would have caught the
original incident on day one; the tests passed throughout because the fixtures
had been written to satisfy the predicate rather than to mirror the API.

### `PopulationPriorOutOfDomain`

`abs(msos_population_prior_mu) > 5 or msos_population_prior_sigma2 > 4` for 5m.

The bound is physical, not statistical: `bayesian_skill` clamps per-wallet edges
to `|edge| ≤ 20` log-odds and caps population variance at `_MAX_SIGMA2_POP =
4.0`. A prior outside that range means the guards themselves regressed. For
scale, the observed bad value was `mu=67.6` — a ~10²⁹ odds ratio.

**First check:** the damping and backtracking line search in
`fit_wallet_posterior`, then the moment clamps in `population_prior_from_fits`.
A fitted hyperparameter fed back into the same fitter is a feedback loop; one
diverging sub-fit destroys the whole population.

### `SkillScoreNotDiscriminating`

`msos_skill_likelihood_saturated_ratio > 0.5` for 15m.

The same incident caught from the output side. Worth having both: a fit can stop
discriminating without the prior leaving its domain, and this rule catches the
failure by its product-visible symptom regardless of mechanism. Healthy steady
state is ~1%; the incident reached 100%.

### `PipelineStalled`

`msos_pipeline_running == 1 and (time() - msos_pipeline_progress_timestamp_seconds) > 1800` for 5m.

A run is in flight but has persisted nothing and crossed no stage boundary.

**First check:** `msos_pipeline_stage_rss_bytes`. Historically this is memory
thrash, not a hang. Then take a `py-spy dump` of the worker before assuming
deadlock or dead sockets — on Windows venvs the console-script `.exe` spawns a
launcher which spawns the real worker, so measure the grandchild with the
multi-GB working set, not the first child.

### `PipelineMemoryHighWater`

`max(msos_pipeline_stage_rss_bytes) > 8e9` for 10m.

Leading indicator for the above. Set the threshold to roughly 70% of the
container memory limit; the shipped default suits a 12 GB instance. Past this
point the failure mode is swap thrash rather than a clean OOM, which presents as
an indefinite stall rather than a crash.

**Mitigation:** reduce `POLYMARKET_WALLET_CONCURRENCY`, then audit any path that
materializes the full activity dataset.

### `UpstreamRequestBuildSlow`

p99 of `msos_upstream_request_build_seconds` > 0.5s over 10m.

Time is going into building requests, not sending them — upstream is not the
problem. Normal build time is microseconds, so this threshold is unambiguous.

**First check:** `msos_http_client_cookie_jar_size`. Per-request cost that grows
with total requests issued is the known signature.

### `HttpClientCookieJarGrowing`

`msos_http_client_cookie_jar_size > 50` for 10m.

The root cause of the above, watched directly. These APIs are cookie-free and
the jar is cleared after every response, so sustained growth means that clear
regressed. The gauge is sampled *before* the clear — sampled after, it would
read zero forever and silently stop guarding the thing it exists to guard.

### `SignalsStale`

No successful shallow run in 3h.

Every downstream artifact — ledger rows, CLV price snapshots, exit diffs,
webhook deliveries — accrues only when ingest runs. Stale ingest silently
freezes all of them, which is why freshness is treated as a product SLO rather
than an operational nicety.

The series is created on first success, so this cannot fire on a deployment that
has never run the pipeline.

**First check:** `INGEST_EVERY_MINUTES` and `GET /ingestor/status`.

### `PipelineRunsFailing`

More than 2 failed runs in 1h. Check `last_error` and `log_tail` on
`/ingestor/status`.

### `FeedLatencyHigh`

p99 of `/signals/skilled-bets` > 30s over 10m.

Requests are falling through to a full recompute — the cold path walked the
whole activity file for ~400s and could exceed 15 minutes.

**First check:** `msos_feed_cache_events_total` for the layer that stopped
hitting. A cold process with no disk artifact is the usual cause.

### `ApiErrorRateHigh`

5xx rate above 5% over 10m.

### `UpstreamErrorRateHigh`

Server/transport error rate to one upstream host above 20% over 15m. Kept
distinct from our own errors because it is actionable by backing off, not by
shipping a fix.

### `RateLimiterSaturated`

Median rate-limiter wait > 2s over 10m. Informational.

This alert exists to head off a specific wrong reflex. Ingest throughput is
bound by a shared upstream budget (`POLYMARKET_API_RPS`, default 5 rps), not by
CPU or worker count. When this fires, workers are queued behind that budget —
raising `POLYMARKET_WALLET_CONCURRENCY` cannot help and risks upstream rate
limiting. Fan-out only helps the recompute path, which reads data already on
disk.

---

## Local setup

```bash
# Scrape target
curl -s localhost:8000/metrics | grep '^msos_'

# Validate rules (requires promtool from the Prometheus distribution)
promtool check rules ops/prometheus/alerts.yml
```

Minimal `prometheus.yml`:

```yaml
global:
  scrape_interval: 30s

rule_files:
  - ops/prometheus/alerts.yml

scrape_configs:
  - job_name: marketsignalos-api
    static_configs:
      - targets: ["localhost:8000"]
```

`apps/api/tests/test_alert_rules.py` asserts that every `msos_*` metric named in
`alerts.yml` is actually registered, so renaming a collector fails CI instead of
silently disarming an alert.

---

## Known gaps

- **No tracing.** OpenTelemetry span propagation across pipeline stages is still
  open. The stage histogram gives durations but not causality, so a slow stage
  cannot yet be attributed to a specific upstream call.
- **Single-process scope.** All metrics are per-process. Once ingest and serving
  split across hosts, `msos_pipeline_*` needs either a push gateway or a
  dedicated scrape target per worker.
- **RSS on Windows.** `process_rss_bytes()` reads `/proc/self/statm` and falls
  back to `resource.ru_maxrss`; Windows has neither, so local dev on Windows
  reports no RSS. Deploys are Linux, where the alerting runs.
