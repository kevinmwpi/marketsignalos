# Full enrichment storage comparison

The offline Parquet reader now feeds the existing two-pass Bayesian enrichment
function. The production ingestion and API read paths still use their existing
stores. This command is the verification gate before enabling a storage change.

Completed experiment: [full snapshot results and limitations](benchmarks/2026-09-08-enrichment-shadow.md).

## Reproduce

Install the ingestor's `benchmark` extra, then run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.enrichment_shadow `
  --data-dir C:/path/to/frozen-jsonl-data `
  --dataset benchmark-output/full-run/dataset `
  --output benchmark-output/enrichment-shadow `
  --memory-mb 512 --threads 2 --rss-limit-mb 4096 --timeout-seconds 3600
```

Use the immutable dataset produced by [the storage benchmark](storage-benchmark.md).
The activity file must match its manifest, including SHA-256, size and modification
time. All five scoring inputs must exist: activity, markets, leaderboard, wallet
hydration and price snapshots. Empty ancillary files are allowed; missing files
are errors. Stop collection or use a consistent snapshot before running.

Output must be a new directory outside the source data and Parquet dataset.
The command copies ancillary context into `output/inputs`, records SHA-256 for all
inputs and Parquet files, then launches each scoring engine in a fresh subprocess.
It rechecks all hashes at completion. It does not construct the ingestion stores,
load the activity deduplication index, call upstream APIs or send notifications.
DuckDB connections explicitly disable extension auto-install and autoload. The
prototype uses explicit-offset observation times without configuring the ICU-backed
`TimeZone` option. An empty-extension-directory subprocess test verifies equivalent
JSONL/Parquet cutoff behavior for positive and negative offsets.

## Equivalence contract

The JSONL baseline uses the same 64-bucket sharder and row parser as production.
The Parquet reader selects one CRC32 wallet bucket at a time, parses rows in
batches, and sorts model references by `source_record` to restore file order.
Sorting raw JSON inside DuckDB exhausted a 512 MiB buffer budget on this snapshot;
the reader now sorts compact ordinal/model references in Python instead. Export order by wallet and
event time is unsuitable for replay: equal-time transactions and out-of-order
observations must keep their original accounting behavior.

Each reader is called twice. Wallet bucket order, first appearance within a
bucket, raw payload conversion and duplicate observations stay the same. This
also preserves the order used for population fitting and emitted bet records.
The adapter checks its file inventory before and after each replay.

The comparison writes canonical JSON for every enrichment and bet record.
Only output `computed_at`, a wall-clock timestamp, is omitted. Every other
field—including nested category scores, ranking, trust flags, tailability,
style, CLV and exact floating values—is compared in emitted order. No numerical
tolerance or row sorting conceals a difference. Output hashes must agree, and
the number of parsed activity records in both passes must equal the manifest.
Skipped activity rows make this complete-coverage check fail.

`report.json` records status, full input fingerprints, code commit plus a hash
of package source, resource settings, per-engine timings and memory, row counts,
and output hashes. A mismatch reports the first differing canonical line.
Workers retain their logs and canonical outputs for investigation. A failure
records `status: failed`; the command exits unsuccessfully. An interrupted
process without a completed report must not be treated as a passing experiment.

## Resource and interpretation limits

- Timings include context loading, activity preparation, both scoring passes
  and canonical output serialization. JSONL preparation includes constructing
  scratch shards; Parquet uses the already-built immutable export. Conversion
  cost is measured separately in the storage benchmark.
- Each worker has a sampled process-tree RSS guard and timeout. The parent checks
  RSS every 50 ms; this can miss brief spikes and is not a container hard limit.
  DuckDB's buffer setting does not limit Python model objects or the OS cache.
- The scorer still materializes each whole bucket. A large wallet or skewed
  bucket can exceed a small cloud instance's memory. This adapter does not solve
  that computational limitation.
- Leave disk space for JSONL scratch shards, canonical bet output, copied context
  and DuckDB spill. On successful JSONL scoring, only that run's scratch shards
  are removed; failed runs retain diagnostics. No automatic production cleanup
  or retention policy is enabled.
- This is a local frozen-snapshot comparison. Cache, engine order and other
  processes affect timings. One paired run cannot establish a stable speedup,
  S3 performance, concurrent API behavior or a Railway memory requirement.
- All available metadata, hydration and resolutions are used. There is no
  historical availability cutoff. Equal scores establish storage equivalence;
  a prospective evaluation is still needed to test predictive value.

Before production adoption, verify incremental snapshot publication, recovery
after interrupted writes, object-store transfer/cost, and bounded scoring of the
largest wallets. Keep the [research evidence requirements](research-credibility.md)
alongside any public performance claims.

The next service boundaries and publication protocol are proposed in
[the cloud separation plan](cloud-separation-plan.md).
