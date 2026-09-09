# Activity storage benchmark

This is an offline prototype, based on `main` at `a90d81b`. It does not replace
the live JSONL stores, delete their deduplication indexes, provision S3, or change
signal rankings. Run a measured experiment before committing to a storage migration.

Completed experiment: [16.9-million-observation results and limitations](benchmarks/2026-09-08-activity-storage.md).

## Reproduce

From the repository root with Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "services/polymarket-ingestor[dev,benchmark]"
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.storage_benchmark `
  --source C:/path/to/polymarket_activity.jsonl `
  --output benchmark-output/full-run `
  --repetitions 3 --memory-mb 512 --threads 2 --rss-limit-mb 2048
```

The source must be stable for the run. Use a stopped collector or a consistent
snapshot; do not benchmark an actively appended file. Output must be a new
directory. Raw data and benchmark scratch files are gitignored.

`report.json` records source SHA-256, byte size and modification time, schema and
DuckDB versions, machine details, workload definitions, conversion cost, every
timed run, correctness checks and the final pass/fail status. Individual child
jobs keep their inputs, results and logs. A failure produces a failed report,
not a speedup claim.

To rerun queries without repeating conversion, use a new output directory and
add `--reuse-dataset benchmark-output/full-run/dataset`. The source fingerprint
must still match. The report marks conversion as reused rather than inventing
a new conversion timing. Query code hashes are saved with the report.

## What is compared

The baseline is a single streaming Python `json.loads` scan, evaluating all
queries together with constant-size accumulators. This matches the full-scan
access pattern used in activity processing without constructing full model
histories in memory. It is **not** a measurement of the live API, its caches,
network fetches, or full two-pass enrichment.

The alternative runs the same aggregate queries over partitioned Parquet using
DuckDB. Each engine gets a fresh child process. Engine order alternates across
repetitions. The OS page cache is not flushed, including after conversion:
neither the first query nor later queries should be described as cold-disk tests.
Connection setup is included in the Parquet workload time; interpreter startup
is separately included in process wall time. Conversion is reported separately.

Wallet selection is deterministic by activity count: largest, median-ranked and
smallest observed wallet. Each has an all-history and a trailing-30-day query;
when observation timestamps exist, each also has an as-of query seven days
before the latest observation. These are access-pattern probes, not a random
population sample. They are selected without inspecting profits or scores.

Each result contains row count, BUY count, sum of observed `usdc_size`, and
minimum/maximum event time. The USDC sum is an integrity statistic, **not**
deduplicated exchange volume, wallet profit, or investable return. Both engines
must match counts/timestamps exactly and sums within relative tolerance `1e-10`
and absolute tolerance `1e-7` before a comparison passes.

## Dataset contract

- A DuckDB staging database permits disk spill while normalizing the source.
  The original JSON payload is retained as `raw_json`, including unknown fields.
- `source_record` preserves source record order. It is not a chain log index.
  A versioned manifest binds the dataset to a source fingerprint.
- Wallet partitioning uses `crc32(lowercase_wallet) % 64`, matching the existing
  enrichment sharding. Sorting within the export improves locality for wallet
  and time predicates. Fixed buckets avoid a directory for every account.
- Partitions use Zstandard-compressed Parquet and 65,536-row target row groups.
  Queries include both the wallet and its bucket, enabling file pruning.
- Invalid required wallet/timestamp/numeric fields fail the build. Records with
  missing or invalid observation timestamps remain in ordinary history but are
  counted and excluded from as-of queries. No time zone is invented.
- Missing size/USDC values project to zero for parity with the legacy aggregate
  scan; their original null/missing representation remains in `raw_json`.
  Audit numeric missingness before using these columns as economic features.
- No deduplication is performed. Legitimate repeated observations and ambiguous
  same-transaction events remain available for a later audited normalization.
- Before publishing the manifest, source and output must agree on row count,
  timestamp extent, missing observation count and order-independent payload/ordinal
  hash aggregates. These DuckDB hashes are integrity checks, not cryptographic
  equality proofs. Tests additionally inspect complete payload round-trips.
- The reader requires the completed manifest and checks expected file sizes.
  Treat outputs as immutable. Same-size tampering is not detected by this reader;
  production object manifests should use object checksums and inventory versions.

Partition pruning is supported by DuckDB's
[Hive partition reader](https://duckdb.org/docs/current/data/partitioning/hive_partitioning),
and column/filter pushdown by its
[Parquet reader](https://duckdb.org/docs/stable/data/parquet/overview).

## Point-in-time research boundary

```python
from pathlib import Path
from marketsignalos_polymarket.activity_parquet import query_activity

result = query_activity(
    Path("benchmark-output/full-run/dataset"),
    "0x0000000000000000000000000000000000000000",
    as_of="2026-07-01T00:00:00Z",
)
```

As-of reads require **both** `event_ts <= cutoff` and `fetched_at <= cutoff`.
An old trade backfilled tomorrow cannot enter yesterday's feature set. This
protects the supplied activity observations only. It does not make current
market metadata, resolutions, prices, scores, or the whole strategy backtest
point-in-time correct. Original `fetched_at` quality still requires an audit.

## Resource measurements and limits

DuckDB's memory setting controls its buffer manager; it is not a process RSS
ceiling. A parent samples the complete child process tree's RSS every 50 ms,
including the interpreter behind a Windows venv launcher, and kills a worker above the
configured RSS guard or timeout, and records failure. Windows additionally
reports peak working set where available. Sampling can miss short spikes and
does not measure the operating-system file cache or provide a container hard
limit. The source file and its existing dedupe index remain untouched.

The staging database, Parquet export and sort spill consume additional disk.
DuckDB spill is capped at 32 GiB; this is not a cap on all output files. Leave
adequate free space and use a container/disk quota for unattended production
experiments. The process retains per-wallet summary metadata, so wallet
cardinality also affects memory use.

Parquet does not eliminate every OOM scenario. Large sorts, joins, aggregates,
writer buffers and skewed whale partitions still require measurements and
limits. See [DuckDB's OOM guidance](https://duckdb.org/docs/current/guides/troubleshooting/oom_errors).

## Migration decision

The **optional Parquet read adapter** and [full enrichment comparison command](enrichment-shadow.md)
are now implemented offline. They validate every score and bet record against JSONL. It does not justify
immediately replacing the ingestion writer or deploying distributed compute.
Before production migration, add:

1. Incremental partition writes, immutable manifests and restart-safe commits.
2. An audited activity identity/deduplication policy, including multi-event
   transactions and revisions; partitioning alone does not provide correctness.
3. Score and bet-ledger parity through the complete enrichment pipeline,
   especially the largest wallet and correlated-event groups.
4. Object-store tests for upload/download time, request counts, regional latency,
   storage charges, compaction and backup restoration.
5. Representative concurrent serving/recompute workloads with enforced resource
   quotas. Only then choose the serving database and worker orchestration.
