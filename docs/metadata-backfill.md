# Bounded metadata backfill and attempt evidence

The 2026-09-12 pass adds a request budget, durable retry state, and inspectable
attempt receipts to the existing activity-driven reference refresh. Qualification
thresholds, coverage semantics, and scoring code are unchanged. Stage 1 is in progress.

## Controls

| Control | Default / ceiling | Behavior |
|---|---:|---|
| Conditions per cycle | 100 | Oldest or never-attempted conditions first |
| Actual HTTP attempts | 8 | Errors count; no internal retries or redirect following |
| Conditions per request | 25 | Separate `closed=true` and `closed=false` filters |
| Response row limit | 100 | A full page is inconclusive, not evidence of absence |
| Retry after returned rows or errors | 1 hour | State survives process restarts |
| Retry after no returned row | 24 hours | Per condition and closed/open filter |
| Endpoint failure | Stop cycle | HTTP/transport errors defer untouched batches too |
| Request receipt retention | 10,000 | Latest condition state survives receipt pruning |
| Inactive condition state | 90 days | Expire only after its recorded cooldown ends |

`METADATA_BACKFILL_MAX_CONDITIONS` and `METADATA_BACKFILL_MAX_REQUESTS` can lower
the defaults. Values outside 1–100 and 1–8 respectively fail validation. A small
budget prioritizes an untouched open filter ahead of retrying the closed filter.
Endpoint cooldowns respect numeric and HTTP-date `Retry-After` headers, with a
one-hour minimum and a one-year sanity ceiling. The worker never sleeps through
that delay. The surrounding pilot deadline still limits the whole process.

Requests use the documented [Gamma markets endpoint](https://docs.polymarket.com/api-reference/markets/list-markets)
with `condition_ids`, `closed`, and `limit`. Missing IDs, unrelated condition IDs,
non-object payloads, and saturated responses are not marked stored or absent.
This validates lookup structure, not the pending metadata/resolution validity
contract. A returned row does not prove a valid settlement label.

## Receipts and failure semantics

The data directory gets `metadata-backfill/attempts.sqlite3`, `latest.json`, and
an OS lock file. SQLite adds no service or package dependency. `latest.json`
reports requests, attempted/deferred/cooldown lookups, rows written, stored and
not-returned lookups, and outcome counts. A **lookup** means one condition under
one closed/open filter. It is not a trade or a distinct global market.
`completed` means selected lookups finished, not complete metadata coverage.
Capped/error/cooldown cycles report `partial`; storage failures raise and write
`storage_error`. The pipeline still returns its existing rows-written integer;
operators inspect this separate receipt for backfill completeness.

Each `attempts` row records the exact condition batch and filter, UTC Unix
timestamps, HTTP status when available, canonical JSON response hash, returned
IDs, and outcome. `conditions` holds the latest per-filter result and next eligible
time. `control` holds the endpoint cooldown. These operational receipts have
explicit retention; they are not a permanent market-observation archive.

A committed `started` receipt precedes transport. A response is not marked
recorded until market/price persistence finishes and confirms every returned row.
Data is persisted once per cycle to avoid rewriting market JSONL for each batch.
Exceptions and short writes leave `storage_error`; a killed process leaves an
uncertain receipt that the next run marks `interrupted`. Underlying writes may
have completed, so recovery never invents an atomic success across both stores.

Condition outcomes distinguish `stored`, `not_returned`, `http_error`,
`transport_error`, `invalid_response`, `response_limit`, `storage_error`, and
`interrupted`. A batch HTTP 404 remains a batch error, not proof that every
condition is nonexistent. Absence from the ledger means **unknown prior history**,
especially after retention. Never label it "never fetched."

The ledger lock prevents overlapping backfills on one host/volume. It does not
replace the pilot's whole-cycle lock or make other legacy writers concurrency-safe.
The cap is not a total memory or dollar cap: full activity scanning, market-store
rewrites, and response bytes still depend on legacy code and the pilot deadline/RSS
guard. Generic catalog pagination retains its existing client retry behavior.

## Isolated diagnostic

The [probe evidence](benchmarks/2026-09-12-metadata-probe.json) and
[notebook](benchmarks/2026-09-12-metadata-probe.ipynb) use the 41 missing condition
IDs associated with the frozen audit's two metadata-only candidates. At
**2026-09-12 08:32 UTC**, four HTTP 200 responses returned **36** distinct conditions;
**five** were not returned under either filter. Returned rows all came from the
closed filter. This establishes current API availability, not historical causes.

| Frozen candidate | Missing IDs | Returned now | Not returned under either filter |
|---|---:|---:|---:|
| `0x521070c99db06e54af5e8e4a91d6858decdfbd53` | 36 | 32 | 4 |
| `0x5a218c7ad04135830a45c41aaed7294df7809318` | 5 | 4 | 1 |

To make a new observation in a **new isolated directory**:

```powershell
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.metadata_probe `
  --audit docs/benchmarks/2026-09-10-metadata-coverage.json `
  --output benchmark-output/metadata-probe-new
```

The command reads the committed audit and writes current raw Gamma rows with
recording timestamps plus exported receipts. It never loads the 8 GB activity
file or its deduplication index. Existing output directories are rejected.
Exit 2 means some conditions are inconclusive due to deferral/errors, not that
markets do not exist. New directories have independent budgets: this is a manual
diagnostic, not a replacement scheduled collector.

Raw responses remain local under `benchmark-output`; their hash and all request
and condition receipts are committed. The notebook recomputes the summary from
those receipts and optionally verifies raw response hashes using
`MSOS_METADATA_PROBE_OBSERVATIONS`. It makes no new network calls. Historical
market and hydration SHA-256s still match the frozen benchmark. No current data
was added to those files; no model fit or gate replay was run in this pass.

## Validation and remaining work

**596 tests passed, one Windows-specific RSS-source test skipped.** Ruff passed
across both packages; root mypy passed Windows and Linux rules (107 files).
Tests cover actual request caps despite configured retries, redirects, rate-limit
headers, transport errors, starvation, restart cooldowns, receipt pruning, invalid
responses, interrupted transport/storage, short writes, OS lock exclusion, runner
integration, and the exact frozen candidate cohort. Notebook code cells were
executed sequentially with repository Python, including local raw-response checks.
No frontend files changed.

Next: separate valid metadata from resolution evidence in a versioned contract
and measure the same-input qualification effect. Present-but-unsettled records
remain a larger issue than these missing IDs. Then finish bounded deduplication
and retention, snapshot serving/recovery, and measure an actual pilot resource
envelope before cloud activation. Prospective paper-following after delays and
costs remains necessary before profitability or ML claims.

No Railway resources, billing settings, paid subscriptions, or schedules changed.
The **$15/month target** remains a design target, not a configured billing cap.
