# Full enrichment replay: JSONL versus Parquet

**Passed:** all 16,889,639 activity rows replayed twice per engine, yielding
833 identical wallet enrichments and 923,705 identical bet records. Output
comparison is exact canonical JSON in emitted order, excluding only generated
`computed_at`. The result covers 843,461 resolved bet records.

| Measurement | JSONL | Parquet |
|---|---:|---:|
| Full worker time | 843.47 s | 532.52 s |
| Activity preparation | 250.06 s | 0.07 s |
| Two scoring passes and canonical output | 581.12 s | 518.12 s |
| Peak worker RSS | 3,146.51 MiB | 3,213.77 MiB |

The observed full-worker ratio was **1.58x** in one local comparison.
JSONL time includes rebuilding its temporary shards. Parquet uses the previously
built 1.095 GB snapshot; conversion is excluded here and measured separately in
[the storage experiment](2026-09-08-activity-storage.md). Input fingerprinting,
final validation and interpreter startup are outside worker elapsed time;
process wall time is also retained in the JSON evidence.

## Failure found and fixed

The first Parquet attempt exhausted DuckDB's 512 MiB buffer budget while sorting
variable-length raw JSON into original source order. The fixed reader fetches
rows in batches, parses the existing model representation, then sorts ordinal
and model references in Python. The full bucket still resides in memory.
A regression exercises large payloads with a 192 MiB DuckDB budget.

The JSONL baseline had already completed successfully before that reader failure.
It was reused after reconstructing and verifying its complete package source
hash with the original adapter. Every other parser, scoring and serialization
module was unchanged. The evidence retains the original adapter source,
original failure, both code fingerprints and an explicit baseline-reuse flag.
All original and frozen input files, Parquet partitions and baseline output
hashes were rechecked before and after the successful reader retry.

The run used a working tree based on `45b63f4`; the recorded package source
SHA-256 identifies the measured implementation, including the then-uncommitted
adapter. The complete package hash was verified again before final validation.
Final validation: **466 tests passed, 1 skipped** (RSS source unavailable on
Windows), with pinned Ruff and Linux-target strict mypy checks passing.

## Snapshot quality findings

Recomputed scores mark 125 wallets trusted and 708 untrusted. Incomplete market
metadata is flagged for 701 wallets. All 833 are blocked by at least one
current tailability gate; zero wallets qualify on this frozen snapshot.
Reasons overlap: conservative edge is not positive for 799, and 632 have fewer
than ten closing-line observations. See the JSON evidence for every reason count.

These are diagnostics of stored observations through June 12, 2026, not live
wallet recommendations or proof about the overall Polymarket population. The
next research work should improve and audit coverage, then run a prospective
evaluation. Changing thresholds to manufacture a nonempty feed would not resolve
missing data or establish predictive value.

## Limits and next decision

- One paired local comparison on a shared Windows host; OS cache was not flushed.
  A reader retry follows the baseline. Timings do not establish a stable cloud
  speedup, object-storage performance, API concurrency or a memory allocation.
- Both engines ran under a 4 GiB sampled process-tree RSS guard, with DuckDB set
  to 512 MiB and two threads. Short memory spikes and OS cache are not fully
  measured. Use hard container limits for deployment qualification.
- Parquet removes repeated shard preparation, but the two-pass stage still
  materializes Python buckets. Profile and reduce that stage before assuming
  that compact storage will fit a small Railway worker.
- The inputs include all available metadata and resolutions, with no historical
  availability cutoff. Storage equality does not validate future predictive edge.
- Production read paths and writers remain unchanged. Paid cloud resources were
  not provisioned.

Reproduce with [the full enrichment command](../enrichment-shadow.md). Machine,
code and source fingerprints, every count, output SHA-256 and resource measurement
are in [the evidence JSON](2026-09-08-enrichment-shadow.json). The proposed next
service boundaries are in [the cloud separation plan](../cloud-separation-plan.md).
