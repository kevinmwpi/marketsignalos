# Stage 0 diagnostic runbook

The [2026-09-10 report](benchmarks/2026-09-10-gate-attrition.md),
[machine-readable evidence](benchmarks/2026-09-10-gate-attrition.json), and
[notebook companion](benchmarks/2026-09-10-gate-attrition.ipynb) complete Stage 0 of
the [handoff blueprint](handoff-blueprint.md). This pass changes no qualification
thresholds, scores, public feed, or live collection configuration.

## Reproduce

Use the canonical `parquet/enrichments.jsonl` (or byte-identical JSONL-engine output)
from the [verified full enrichment shadow](enrichment-shadow.md). It contains all
833 wallets, not a sample of winning wallets. Only `computed_at` was excluded by
the original canonical exporter. The SHA-256 is
`01564d095e7945cee89895f3f278a0f0d66e9aee6b88530325f27e10a447dc5f`.

Install both local packages and their development dependencies as documented in
`CLAUDE.md`. From the repository root, substitute the frozen score file path:

```powershell
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.gate_attrition `
  --snapshot C:/path/to/enrichment-shadow/parquet/enrichments.jsonl `
  --benchmark docs/benchmarks/2026-09-08-enrichment-shadow.json `
  --output benchmark-output/stage0-rerun/gate-attrition.json
```

The output must be new. The command creates JSON and Markdown without overwriting
either source or an existing report. It uses only the standard library, caps each
input at 128 MiB, and never constructs collection stores or loads the multi-GB
activity deduplication index. The notebook takes the same input through
`MSOS_ATTRITION_SNAPSHOT` and compares every generated analysis field with the
committed receipt. The Markdown conclusion below the generated report is reviewed
interpretation; the generated JSON contains the measurements and provenance.

## Method

The unit is one wallet's `forecast-v4` score from the frozen historical cohort.
The command verifies SHA-256 and byte length against **both** shadow engines,
checks wallet counts and unique case-normalized keys, rejects unknown versions,
unknown or duplicate rejection reasons, missing/nonfinite metrics, contradictory
statuses, and metrics incompatible with the serialized rounding interval.
The report records the scoring code identity from the original benchmark and the
current diagnostic source hash. Both inputs are rehashed after analysis.

Baseline decisions come from stored reasons because production evaluates gates
before rounding numeric fields. Gate 10 combines three economic reasons. Gate 12
is not evaluated after inadequate recent sample; the report counts those skips
explicitly. Gate 13 requires both adequate sample and a positive CLV lower bound;
reducing its sample threshold does not waive the confidence requirement. Ambiguous
rounding boundaries produce minimum/maximum counts rather than guessed decisions.

The JSON includes all failure patterns, pairwise gate overlaps, the number of
model/economic failures per wallet, and descriptive percentiles for all wallets
and the data-trusted subset. Edge values are log-odds adjustments, not percentage
point returns; CLV values are differences in outcome prices. The score snapshot
is historical (the linked activity benchmark ends in June 2026), not current data.

## Engineering verification

- Final local run: **559 tests passed, one Windows RSS-source test skipped**.
  Ruff passed for both packages; repository-wide mypy passed for native Windows
  and with Linux platform rules (99 source files). The notebook's three code cells
  were executed sequentially with repository Python and reproduced the committed
  analysis. No frontend code changed or frontend build was rerun in this pass.
- An initial full-suite run correctly failed its scoring-code integrity guard when
  the diagnostic module was formatted during the experiment. The final full run
  above used stable code and passed. The guard was preserved.
- Gate tests include hand-calculated overlap/counterfactual fixtures and direct
  calls to the production scorer around rounding and conditional-rule boundaries.
- API deep-run options now use explicit typed keywords. A new repository-wide
  mypy CI job covers both packages and tests, including API-to-ingestor imports.
- DuckDB auto-install/autoload are disabled and the ICU-backed connection option
  is removed. A subprocess using an empty extension directory confirms JSONL and
  Parquet agree for equivalent UTC/positive/negative-offset timestamps and excludes
  a record one second after the cutoff. No full 17-million-row benchmark was rerun.
- The Unix-only RSS fallback is guarded on Windows; macOS peak RSS is already in
  bytes and is no longer multiplied by 1024.
- Alloy v1.19.2 (`becfd48`, Windows/amd64) was downloaded from the official release
  and checked against its published `SHA256SUMS`. Archive SHA-256:
  `5c4d83acb0295cb30012c68eefd0f1e90f53f9bca5b593961aeb8312b4d86754`.
  `alloy fmt -w`, `alloy fmt -t`, and `alloy validate` exited 0 using placeholders,
  including `MSOS_METRICS_BEARER_TOKEN`. Formatted config SHA-256:
  `2b44f775ed3b2895b34cd65436713ac8ab402123a93834367bb124a872a3d5d8`.
  The Dockerfile pins v1.19.2. No Alloy service was started or live scrape attempted.

## Remaining and cloud status

Next, explain missing metadata for the two candidates blocked solely by that gate:
unresolved/missing markets, lookup failures, unsupported records, and denominator
semantics. Keep the other 831 wallets' model/economic failures visible. Do not lower
the global metadata or CLV thresholds to fill the feed. Complete branch review and
merge requirements before Stage 1; the implementation remains on the feature branch.

Later work remains: bound long-term deduplication memory and retention, wire the API
to complete immutable publication generations, prepare the Railway worker/serving
deployment and recovery checks, measure actual resource use, then freeze a cohort
for prospective paper-following with realistic delays and costs. ML comes later.
This pass incurred no new Railway resources or plan changes. The $15/month target
remains a budget constraint, not a measured bill or a forecast of trading income.
