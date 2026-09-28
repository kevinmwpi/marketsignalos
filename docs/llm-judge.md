# LLM judge for the Polymarket → Kalshi market matcher

**Status (2026-09-28):** Phase 1 (eval set and baseline tooling) is built and tested.
The baseline report does not exist yet, because it needs your market data and your
labels (see [Running Phase 1](#running-phase-1)). Phases 2 and 3 are not started.
Nothing in Phase 1 calls a model.

## Why this exists

The matcher links a Kalshi market to a Polymarket market when their titles look alike.
An approved link feeds the Kalshi mirror price shown next to a signal. If the link is
wrong, the dashboard shows a price for a *different bet*. That is worse than showing
no mirror at all. So the goal is **precision**: of the links the system approves by
itself, how many are right? Recall matters, but less.

Today, pairs scoring 0.35–0.75 on title similarity wait for a human in
`review-matches`. The judge will look at those pending pairs only, read both markets'
rules, and approve or reject the pairs it is confident about. Before that can be
trusted, there has to be a way to measure it. Phase 1 builds that measuring stick.

## What "same event" means

Every label answers one question:

> If you bought YES on the Kalshi market and YES on the Polymarket market, would the
> two positions **always pay out the same way**?

Yes only if all of these hold:

- **Same proposition.** The same thing has to happen.
- **Same threshold or bracket.** "Cut of 25 bps" and "cut of 50 bps" are different
  events, even though both are "the September Fed decision".
- **Same deadline.** "By September 30" and "by December 31" are different events.
- **Compatible resolution source.** If one settles on the BLS release and the other on
  a news report, and those could disagree, the answer is no.
- **Same direction.** If one market is the other's inverse ("Will X happen?" against
  "Will X *not* happen?"), label it **false**. The downstream code maps YES to YES, so
  an inverse pair would show the wrong side's price.

When in doubt, label false. A missed match costs a mirror price; a wrong match shows
a misleading one.

## Running Phase 1

Run these from the repository root on a machine that has your pipeline data and can
reach `api.elections.kalshi.com` and `gamma-api.polymarket.com`. Replace `<data>` with
your `POLYMARKET_DATA_DIR` if it is not the default `services/ingestor/data`.

```bash
M="uv run python -m marketsignalos_polymarket.matching_eval"

$M verify-fields --data-dir <data>     # 1. confirm both exchanges' rules fields (one call each)
$M seed          --data-dir <data>     # 2. import every manual decision from market_links.jsonl
$M sample        --data-dir <data>     # 3. queue ~50 unlabeled pairs
$M label                               # 4. label them yourself (resumable)
$M run                                 # 5. write reports/tfidf-baseline.{json,md}
```

`sample` needs `kalshi_markets.jsonl` and `polymarket_markets.jsonl`. The Kalshi step is
off by default in the pipeline, so run `fetch-kalshi-markets` (or the pipeline with
`--include-kalshi`) first if you haven't recently.

## Phase 1 design choices

### Files

| File | What it is |
|---|---|
| `evals/market_matching/cases.jsonl` | The eval set: labeled pairs only. Committed, so every change is reviewable. |
| `evals/market_matching/to_label.jsonl` | The queue of unlabeled pairs from `sample`. |
| `evals/market_matching/to_label.manifest.json` | How the queue was drawn: seed, quotas, input file hashes, matcher settings. |
| `evals/market_matching/reports/` | Eval reports, JSON and Markdown. `tfidf-baseline.*` is the baseline. |
| `.../marketsignalos_polymarket/matching_eval.py` | Case format, validation, scoring, reports, CLI. |
| `.../marketsignalos_polymarket/matching_cases.py` | Seeding, sampling, labeling. |
| `.../marketsignalos_polymarket/market_rules.py` | Fetches one market's rules text by id. |

### Only you write labels

The code never decides whether a pair is the same event. `seed` copies decisions you
already made in `review-matches`. `sample` writes pairs with `label: null`. Only
`label` records a new label, and only from your keystroke.

### Imported decisions don't get invented reasons

`review-matches` recorded approve or reject, but never *why*. Seeded cases say exactly
that: *"Imported review-matches decision (approved); no reason was recorded at review
time."* Making up a plausible reason would mean the code labeling on your behalf. You
can edit these reasons later; the report splits results by where a label came from, so
imported and hand-labeled cases can be compared.

### The baseline replays production; it doesn't re-score

TF-IDF weights a word by how rare it is across *all* markets at the moment the matcher
runs. So a single pair can't be re-scored in isolation and get the same number
production got. Instead, each case records what production actually decided for it:
approved, pending, or dropped. The TF-IDF "matcher" in the eval reports that recorded
decision back. This is what makes the baseline honest: it measures the matcher you
actually run, not an approximation.

### The sampler proves it matches production

The sampler loads markets with the same functions production uses, applies the same
parlay filter, calls the real `match_markets`, and then recomputes every score itself.
It **asserts that its scores equal production's for every link**. If they ever differ,
sampling stops with an error instead of quietly building an eval for a different
matcher. A test deliberately breaks this and checks that it stops.

### Rules text is frozen into each case

Both exchanges can edit a market's rules after it's listed. If an eval case pointed at
live rules, an old label could silently become wrong. So each case stores the rules
text as it was when fetched, and the time it was fetched. You label while reading the
same text the judge will later read.

To make that possible, Phase 1 already includes a small fetch-by-id for rules text
(`market_rules.py`). That pulls a piece of your step 4 forward. The Phase 2 part of
step 4, storing rules in the pipeline's own market files so the judge has them at run
time, is still to do.

### Titles and dates come from what production saw

For each side, the title and end date come from your stored market records, because
those are what the matcher compared. Only the rules text (and fields the stored record
lacks) comes from the live fetch.

### Kalshi bracket text is stored and shown

Kalshi splits one event into brackets that often share a title word for word. For
example, "Fed rate cut at September 2025 meeting" with brackets "Cut of 25 bps" and
"Cut of 50 bps". The bracket lives in `yes_sub_title`, which the production matcher
never reads. So TF-IDF scores both brackets identically against a Polymarket "25 bps"
market and can approve both. Each case stores the bracket as `kalshi.subtitle`, and
the labeler shows it on its own line. This is likely the single most common way the
current matcher is wrong.

### Rules field names: one verified, one guarded

- **Polymarket.** A real Gamma response recorded in `docs/polymarket-api-discovery.md`
  shows `description` (the rules) and `resolutionSource` on every market row.
  **Verified from an observed payload.**
- **Kalshi.** I could not reach Kalshi's API or docs from the build environment, so
  `rules_primary` / `rules_secondary` come from the documented v2 market object,
  **not verified against a live response**.

Neither name is trusted blindly. If a real response lacks the expected keys, the fetch
stops with an error that lists the keys it *did* find. So a wrong name fails your
first run loudly and can't produce blank rules. `verify-fields` runs that check on one
market per exchange before you do anything else.

### The sample is stratified, and near-misses come first

A random sample of candidate pairs would be almost all obvious non-matches and teach
nothing. So the ~50 pairs are drawn from five groups, called strata, with fixed shares:

| Stratum | Share | What it tests |
|---|---:|---|
| Near misses | 24% | The known false-positive shapes (below). Most important for precision. |
| Auto band (≥ 0.75) | 20% | Are today's automatic approvals actually right? |
| Pending band (0.35–0.75) | 28% | Where the judge will work. |
| Below band (0.20–0.35) | 14% | True matches the matcher silently drops. |
| Pre-filter misses | 14% | True matches the matcher never even compares (below). |

If a group has too few pairs, its leftover share goes first to the pending band, which
is where the judge will operate.

**Near-miss kinds** (a pair can have several):

- `sibling_bracket`: two brackets of one Kalshi event both pair with the same
  Polymarket market.
- `numeric_diff`: the titles differ only in numbers ("100k" against "120k", "25bp"
  against "50bp").
- `date_gap`: similar titles, but end dates more than a day apart.

**Pre-filter misses.** Before scoring, the matcher only compares markets in the same
rough category whose end dates are within 3 days. A true match that fails that filter
can never be found, and no judge downstream can fix it. The sampler also looks outside
the filter, at pairs sharing at least two informative title words with similarity
≥ 0.5, to measure how often that happens. Kalshi's `expiration_time` is often days
after the event itself, so the date window is a plausible source of misses.

**No single event dominates.** At most 2 pairs per Kalshi event, so one Fed meeting's
brackets can't fill the sample. Within each group, pairs are spread across categories.

**Deterministic.** Same input files, seed and existing cases give the same sample. The
seed and input file hashes are written to the manifest.

### Validation fails closed

If any line of `cases.jsonl` is malformed, the whole file is rejected with the line
number. Skipping a bad line would silently change the number every metric is divided
by, and a precision figure that moved for that reason would be misleading. Reasons
must be one line, 240 characters or fewer. Case ids must match the two market ids, and
no id may repeat.

### What the report measures

Every matcher returns one of three answers per pair: **approve**, **reject**, or
**pending** (leave it for a human). Against your labels:

| Metric | Meaning |
|---|---|
| **Precision** | Of the pairs approved automatically, the share that are truly the same event. The headline. |
| 95% interval | The range precision plausibly sits in, given how few approvals there are. |
| Recall | Of the true matches, the share linked automatically. Pending counts as not linked. |
| Recall reaching review | Of the true matches, the share at least put in front of a human. |
| Abstention | The share left pending: your review workload. |
| Reject precision | Of the rejected pairs, the share truly different. |

The report lists **every** wrong decision (a wrong approval or a wrong rejection) with
both titles, the bracket, dates, and your label reason. It also lists the pending
pairs, and breaks results down by band, near-miss kind, and label origin.

**Why an interval and not just a percentage.** With about 50 cases there may be only
10–15 automatic approvals. One extra mistake then moves precision by 7–10 points. The
interval (a Wilson score interval, which behaves sensibly at 0% and 100%) shows how
much the number can be trusted. The report warns when there are fewer than 20
approvals.

**Why "no approvals" shows n/a, not 100%.** A matcher that approves nothing has made no
mistakes, but it hasn't shown it is precise either.

### Known limits of Phase 1

- **Recall is measured only within the labeled set.** The sample comes from pairs the
  matcher scored at least moderately, or that share title words. True matches with
  unrelated wording are never sampled, so real recall is lower than reported. Precision
  doesn't have this problem, and precision is the priority.
- **Imported cases carry production's decision at review time.** Seeded cases take
  their confidence from `market_links.jsonl`. If you later change the matcher's
  thresholds, re-sample instead of reusing old recorded decisions.
- **Small n.** About 50 hand-labeled cases plus your imported decisions. Enough to catch
  a precision collapse, but not enough to separate 92% from 96%.

## Phase 2: the judge (not started)

The plan follows your spec: pending band only; JSON verdicts checked in code (invalid
output means no decision); approve or reject only above a confidence threshold, with
`matched_by="llm"`; model and prompt version recorded on every decision; a per-pair
cache; off by default; and an eval comparing TF-IDF alone against TF-IDF plus the judge.
Three things to settle at the Phase 1 review:

1. **Model and cost.** Each verdict reads two titles, two dates and two rules texts,
   roughly 2–4k input tokens and a short answer. At current list prices that is roughly
   **$0.02 per pair on Claude Opus 5.5** ($4 / $20 per million input/output tokens),
   about half that on Claude Sonnet 5.5, and about a quarter on Claude Haiku 4.5. Thanks
   to the per-pair cache, each pair is judged once, ever. So monthly cost is about
   *new pending pairs per month × price per pair*. At 500 new pairs a month, Opus is
   about $10, most of the $15 budget. I'll default to Opus 5.5 because precision is the
   goal, and the Phase 2 eval can measure whether a cheaper model keeps precision.
   Which model to use is your call.
2. **"Fixed sampling settings".** On Claude Opus 5.5, `temperature` and `top_p` can't
   be set at all; the API rejects them. Pinning therefore means pinning the exact model
   id, the effort level, the output limit and the prompt version, all recorded on every
   decision. Even then, repeated calls aren't guaranteed identical, which is one more
   reason the cache matters: a pair's verdict is decided once and replayed.
3. **Scheduled workflows.** The only scheduled GitHub Action today is
   `research-snapshot.yml`. It never runs the matcher. The judge will still need two
   explicit opt-ins: `MATCHER_LLM_JUDGE=1`, plus a separate
   `MATCHER_LLM_JUDGE_ALLOW_SCHEDULED=1` before it will run inside a scheduled Actions
   job. A test will check this.

How approved links feed signals stays unchanged until you have reviewed the Phase 2
numbers.

## Phase 3: the feedback loop (not started)

`review-matches` will show the judge's decisions first: lowest confidence, and
disagreements with TF-IDF, at the top. Your approve or reject is saved as manual and
also appended to `cases.jsonl`. A test will fail when precision drops below the last
saved baseline. It must compare on the *same* case set: when cases are added, the
baseline is re-run on the new set before comparing, or the test would move for
reasons unrelated to the judge.
