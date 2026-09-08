# Evidence required for a credible research platform

MarketSignalOS should make claims that a reviewer can reproduce and falsify.
Its engineering value does not depend on finding profitable accounts. A
well-measured null result is a research result; a saturated score is a model
diagnostic, not proof that every wallet has alpha.

The supplied work summary says the repaired empirical-Bayes variance reached
its floor and CLV evidence survived. Treat that as a reported result requiring
a dated, reproducible cohort export before turning it into a public claim.
This storage experiment does not re-estimate alpha or validate the CLV finding.

## Priorities

| Priority | Deliverable | Evidence that completes it |
|---|---|---|
| Before public launch | Private operator endpoints | Anonymous ingest/watchlist/webhook mutations rejected; authenticated metrics scrape verified |
| 1 | Activate existing monitoring | Scrape history, rule evaluation, routed notification and recovery receipt |
| 2 | Benchmark storage before migration | Reproducible source fingerprint, equivalent outputs, latency/RSS/storage measurements |
| 3 | Publish a data and methodology card | Coverage interval, source lag, exclusions, schema versions, failure cases |
| 4 | Freeze a prospective evaluation | Versioned cohort, score version, baseline, costs, endpoints and evaluation date |
| 5 | Improve storage incrementally | Shadow parity, crash recovery, object-storage cost and largest-wallet tests |
| 6 | Add ML experiments | Point-in-time data, chronological holdout, reproducible baseline comparison |

The monitoring code already exists on `main`; the deployment/account activation
is separate. The benchmark and dual-time activity query in
[storage-benchmark.md](storage-benchmark.md) are implemented offline. The
evaluation protocol below is proposed, not an already completed experiment.

## Engineering evidence to publish

Maintain a small public status/methodology page showing last collection time,
covered block or source intervals, wallet history completeness, and unresolved
gaps. Link an architecture diagram, incident writeups, SLO definitions and a
sanitized dashboard. Record availability, p95 serving latency, ingestion lag,
cost per million observations and peak memory on named workloads. Specify
whether a measurement is local, staged or live.

Version schema, query code and dataset manifests. Keep raw evidence immutable
and derived features rebuildable. Require idempotent replay, deterministic
ordering, reorg/revision handling, timestamp lineage and documented semantics
for transfers, split/merge, redemption and fees. A JSONL-to-Parquet export of
Data API observations should not be described as a complete raw chain archive.

Use restore drills and fault injection to test operations. An alert rule,
successful scrape, rule firing and delivered notification are four distinct
verification states. Testing a contact point alone does not establish that the
data or rule evaluation path works.

## Quantitative evaluation protocol

**Primary question:** does a frozen wallet-selection rule identify future
positions whose performance exceeds a declared price-based or matched-account
baseline after realistic follower costs?

Before looking at the evaluation interval, save a versioned configuration with:

- Discovery source, cutoff and all screened accounts, including losing and
  inactive accounts. Record qualification and exclusion reasons for each.
- Model/prior version, training interval, category rules and exact score gates.
  The cohort stays fixed for that evaluation. Track all tried variants.
- One primary outcome and horizon. For example, event-weighted signed price
  improvement over a predeclared horizon using contemporaneous executable
  quotes. Settlement ROI and calibration can be secondary outcomes.
- A price baseline and a matched comparison cohort chosen using information
  available at the cutoff, such as category, activity and size. Specify the
  matching procedure and report remaining imbalance.
- Entry delay, executable side of the book, size, spread, fees and slippage.
  Keep wallet-entry PnL separate from follower PnL. Exclude non-executable
  observations with reasons; do not convert missing quotes to zero cost.
- Event-level dependence handling, sufficient-sample/power rationale and a
  fixed evaluation date. Unresolved events remain censored/open under a
  predefined policy; they are not silently removed as if random.
- Confidence intervals and multiple-comparison policy for any account-level
  claims. Posterior skill likelihood is not a trade win probability or an
  adjusted population significance level.

Use event clusters for resampling so repeated fills and related outcomes do
not masquerade as independent observations. For exploratory categories and
accounts, disclose the number screened and control false discoveries using a
method whose dependence assumptions fit the experiment. Sequential monitoring
needs predefined looks or a suitable sequential procedure; otherwise a frozen
evaluation date is easier to audit.

Test model-implied probabilities with calibration plots, Brier score and log
loss on later unseen data, alongside economic outcomes and drawdowns. Include
ablation comparisons: price-only, CLV-only, historical edge, and combined model.
Record null results and failed variants. Repeated model selection on the same
history can inflate apparent performance; see Bailey and coauthors'
[Probability of Backtest Overfitting](https://carmamaths.org/resources/jon/backtest2.pdf).

## Public product language

Describe accounts as “observed wallets” and scores as “historical forecasting
evidence.” Explain whether a wallet has sufficient data and whether a new
position still offers a usable price. Do not identify a human, assert insider
information, or promise that following a skilled account improves a specific
trade's win probability without validating that predictive claim.

Show a dossier with score components, effective event count, timestamps,
coverage, sensitivity to priors and later performance. Publish the methodology
and failure modes beside rankings. Let users distinguish an interesting account
from a validated forecasting signal and from an executable trade.

## ML learning path

Use the dual-time activity adapter as one input to versioned feature snapshots;
apply the same availability-time policy to quotes, metadata and resolution
labels. Start with a price-only baseline and logistic regression, then consider
gradient-boosted trees. Split chronologically, group related events and prevent
future wallet selection from leaking into earlier training rows.

Keep a final holdout untouched. Save feature definitions, dataset hashes,
training configuration and model artifacts. Promote a model only after later
unseen evaluation and a shadow run support it. Distributed training or a more
complex model can wait until simpler baselines identify a useful signal.
