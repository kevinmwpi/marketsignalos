# Snapshot-consistent metadata coverage

The [frozen audit](benchmarks/2026-09-10-metadata-coverage.md),
[JSON evidence](benchmarks/2026-09-10-metadata-coverage.json), and
[notebook](benchmarks/2026-09-10-metadata-coverage.ipynb) continue Stage 1 of the
handoff blueprint. This is a coverage freshness repair on the feature branch;
the definition of a covered market and every qualification threshold stay unchanged.

## What was wrong

The scorer loaded a wallet's saved hydration coverage even when the market file
had changed since that coverage was computed. Collection refreshes coverage, but
offline scoring and independently scheduled scoring can consume stale counts.
On the verified 833-wallet snapshot, 537 saved count/ratio tuples disagree with
the supplied activity and market files. The scorer now derives coverage from those
same inputs, reusing its existing wallet shards, and replaces only the in-memory
metadata fields. It does not modify the hydration file or its other trust flags.

The existing rule counts a condition as covered if any stored market observation
is closed or has an outcome price at least 0.99. This conflates metadata presence
with a settlement proxy: open, unsettled markets can have records and still fail.
A closed flag or near-one price also does not prove that a resolution label is
valid. This pass preserves that rule and reports the distinction explicitly.

## Measured result

Across 758,510 distinct wallet-condition pairs, 737,950 pass the existing rule,
20,352 have market records but fail the settlement proxy, and 208 lack market rows.
These are pair counts: a shared market can appear for multiple wallets.

Replaying only the metadata gate on the original scores changes metadata failures
from **701 to 592**, data-trusted wallets from **125 to 226**, and tailable wallets
from **0 to 0**. Both metadata-only candidates remain blocked. They have 41 distinct
missing market conditions in total, alongside 3,443 present-but-unsettled conditions.
Their stored coverage improves from 38.92% to 94.09% and from 68.14% to 87.33% when
recomputed, but neither reaches the unchanged 100% gate.

The historical files do not record whether a missing row was never requested,
omitted by Gamma, lost to an HTTP error, or left behind by an interrupted or capped
backfill. Those causes remain **unknown**. Positions pagination and missing category
labels are not inputs to this coverage calculation; this audit does not establish
either as the cause. No current API response was mixed into historical evidence.

## Reproduce

Install the ingestor's `benchmark` extra. Use the same verified shadow scores and
Parquet dataset as Stage 0, plus the matching historical markets/hydration files:

```powershell
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.metadata_audit `
  --snapshot C:/path/to/enrichment-shadow/parquet/enrichments.jsonl `
  --benchmark docs/benchmarks/2026-09-08-enrichment-shadow.json `
  --data-dir C:/path/to/frozen-context `
  --dataset C:/path/to/activity-parquet-dataset `
  --output benchmark-output/metadata-audit-rerun `
  --memory-mb 512
```

The output directory must be new. It receives `report.json`, `report.md`, and
`diagnostic-replay.jsonl`. The latter is explicitly a diagnostic artifact; do not
copy it into a production score directory. It preserves every numeric score and
non-metadata rejection decision, so this is a same-input gate replay rather than
a new fitted model or full enrichment benchmark. Production integration is tested
through both the single-wallet and streaming scorers.

The audit verifies score hashes against both shadow engines, verifies market and
hydration hashes, and hashes the manifest plus all 64 Parquet files before and
after analysis. It streams distinct wallet-condition pairs in one bucket at a
time with a 512 MiB DuckDB limit and one thread. That setting is not a cap on total
Python process memory. The collector's multi-GB deduplication index is never loaded.
The original 8 GB activity JSONL is not reread; verified Parquet files link back to
its recorded fingerprint.

The notebook uses `MSOS_ATTRITION_SNAPSHOT`, `MSOS_METADATA_DATA_DIR`, and
`MSOS_ACTIVITY_DATASET` for the three local source paths. It replays the audit and
compares its counts, per-wallet detail, and before/after gates with the committed
evidence. No sources need to be uploaded or written to a cloud service.

## Remaining work and budget

Separate metadata validity from resolution evidence in a versioned coverage
contract, with a before/after qualification audit. The
[2026-09-12 backfill pass](metadata-backfill.md) adds bounded requests, durable
attempt receipts, and a retry policy. An isolated current lookup returns 36 of
the two candidates' 41 missing IDs; five are absent from both filtered responses.
Historical absence causes remain unknown. Further changes must not silently alter
gates 7–13 or substitute present-day prices
for historical observations. These remain Stage 1 work; no candidate is being
promoted to the public feed by this pass.

Cloud readiness still needs bounded deduplication storage and retention, complete
snapshot serving, recovery checks, and an actual resource/billing measurement.
The $15/month target and prepare-before-provisioning instruction remain in effect.
No Railway resources, billing settings, or active collection schedules were changed.
Prospective paper-following evidence after delays and costs is still required
before interpreting the analytics as a profitable strategy; ML remains later work.

## Validation for this pass

- **568 tests passed; one Windows-only RSS-source test skipped.** Ruff passed
  across both packages, and root mypy passed under native Windows and Linux
  platform rules (103 files). No frontend code changed or frontend build was run.
- New tests exercise stale-low and stale-high coverage through real scorers,
  preserve unrelated trust/economic inputs, include SELL-only conditions, deduplicate
  fills, and compare the shared coverage calculation with collection-time hydration.
- A small end-to-end audit fixture verifies the same-input replay and read-only
  behavior. Changed context or Parquet bytes are rejected, and an existing report
  directory cannot be overwritten.
- The full 833-wallet audit checked 65 Parquet/manifest hashes at both ends and
  verified the score/context receipts. A notebook replay compares all wallet detail
  and both waterfalls. This is not a rerun of the full two-pass enrichment benchmark
  and does not establish current market coverage, live cloud behavior, or trading returns.
