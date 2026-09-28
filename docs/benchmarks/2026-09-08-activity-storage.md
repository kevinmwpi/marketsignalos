# Activity storage experiment — 2026-09-08

**Result: parity checks passed.** On this local workload, median query-bundle
time fell from **82.244s** for a streaming JSONL scan to
**0.287s** over Parquet (**286.6×**).
The output occupied **1.095 GB**, versus
**8.094 GB** of input JSONL
(**7.39×** smaller).

This supports a Parquet read-adapter experiment. It does not establish improved
live API latency, faster full scoring, profitable predictions, or the elimination
of out-of-memory failures.

## Dataset and correctness

- 16,889,639 activity observations from 833 wallets; 64 CRC32 wallet buckets.
- Event extent: 2022-01-06T04:23:05+00:00 through
  2026-06-12T02:25:42+00:00.
- Latest collection timestamp: 2026-06-12 03:01:46.792803+00. This is a historical local snapshot.
- Original payloads retained, including unknown fields and duplicates. No source files or indexes modified.
- A full-payload follow-up check found no fractional/non-integer timestamp representations.
  The [saved query plan](2026-09-08-query-plan.txt) confirms one of 64 files is read for a wallet filter.
- Source SHA-256: `0d88dca29b477f0fc55b5abf9d561fcfbe7e664688a01eedea95ff6bcd1386dc`.
- Row counts, time extent, observation-time coverage and payload/ordinal hash
  aggregates matched on export. Every query repetition matched exact counts
  and timestamps, and sums within relative `1e-10` / absolute `1e-7` tolerance.
- The three selected wallets contain 1,003,120, 349 and 1 observations, chosen
  by activity count, not performance. Selection is deliberately not representative
  of all accounts or workload shapes.

## Measurements

Each run evaluates the same nine-query bundle: all-history, trailing-30-day,
and historical-cutoff aggregates for each selected wallet. Python scans the
input once for the whole bundle. DuckDB runs nine filtered aggregate queries.

| Repetition | Engine | Bundle seconds | Peak RSS, MiB |
|---|---|---:|---:|
| 1 | jsonl | 82.915 | 44.5 |
| 1 | parquet | 0.287 | 54.6 |
| 2 | parquet | 0.282 | 54.5 |
| 2 | jsonl | 82.244 | 44.3 |
| 3 | jsonl | 81.527 | 44.3 |
| 3 | parquet | 0.294 | 54.7 |

The full initial conversion took **194.050s**
including source hashing, staging, sorting, compression and integrity checks.
Windows reported **741.8 MiB**
peak interpreter working set. DuckDB used a 512 MiB buffer setting and two threads.
The query adapter uses slightly more RAM than the streaming JSONL baseline;
the demonstrated improvements are latency and stored bytes, not lower query RSS.

## Limits and development findings

1. The OS cache was not flushed. Engine order alternated and child processes
   were fresh, but these are cache-influenced local runs, not cold-disk or S3 tests.
   The shared Windows host was not isolated; tests overlapped part of the first
   repetition. All measurements are retained rather than selecting the fastest.
2. All three historical-cutoff queries return zero: their cutoff precedes the
   observation timestamps. This is the correct availability-time behavior, but
   not evidence of a successful historical strategy evaluation. Separate tests
   cover non-empty as-of results and late-arriving records.
3. The first conversion revealed a Windows wrapper-process monitoring gap.
   Its RSS number above comes from the actual interpreter's OS peak, not the
   wrapper sampler. The sampler now measures and terminates the entire child
   tree; a regression test exercises the memory guard. Final query timings above
   use the corrected implementation and the previously verified dataset.
4. Raw activity is not a full chain archive. No production deduplication migration,
   S3 transfer/cost measurement, concurrent serving or complete scoring run was performed.
5. A 512 MiB DuckDB setting is not a hard RSS ceiling; sorts, buffers, metadata
   and skew remain relevant. The new sampler is a guard, not an OS-enforced quota.

## Reproduction and next decision

See [commands and methodology](../storage-benchmark.md),
[machine-readable evidence](2026-09-08-activity-storage.json), and the benchmark
module `marketsignalos_polymarket.storage_benchmark`. The evidence includes code
hashes, source fingerprint, machine details, individual timings and query results.

Validation: **459 passed, 1 skipped** on Windows. The skip is the existing
platform-dependent RSS-source test. Package lint and strict type checks pass;
type checks target Linux, matching deployment/CI. CI now installs the benchmark
extra and exercises its correctness tests on small fixtures.

Next: implement an opt-in Parquet read adapter and shadow full enrichment against
JSONL, including the largest wallet. Require score/ledger parity and bounded
resource use before changing production storage. Complete live monitoring and
freeze a prospective evaluation before presenting the tool as validated alpha.
