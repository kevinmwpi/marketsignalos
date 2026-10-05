# Gate 13 on 1-hour post-entry CLV (`forecast-v5`): plan for review

Status: **decisions chosen, build not started.** Written 2026-10-05 after the
horizon rule decided h = 1 hour (blueprint §12, decision 6). The owner chose the
recommended option for D1–D5 on 2026-10-05 (§3); the build follows §4.

## 1. What is settled

- Gate 13's reference price is the market's price **1 hour after each buy**
  (decision 6 and its three amendments). The last pre-close price is rejected: it
  sits at the outcome for 85% of the same bets.
- The leakage bound held at 1 h on the population the rule measured: fills at least
  seven days before the market's scheduled end, on resolved binary markets.
  `near_outcome` was 0.049 on the common set and 0.0496 on all referenced bets in
  the deciding report, and 0.036 and 0.038 eight hours later.
- The gate's form stays: `clv_sample >= MIN_CLV_SAMPLE` and `clv_lower_bound > 0`.
  Only the reference changes in the first commit (invariant 1: a gate change goes
  in its own commit, with before/after cohort counts).

## 2. The definition

For each BUY fill *f* of a bet (wallet, condition, outcome):

```
ref_f   = YES price at t_f + 1 h (entry_prices.price_after, 2 h tolerance); NO = 1 - YES
clv_f   = ref_f - price_f            price_f = usdc_f / size_f (the price the wallet paid)
clv_bet = sum(usdc_f * clv_f) / sum(usdc_f)   over the bet's fills that have a reference
```

Wallet level: the existing `_weighted_clv_stats` — event-capped capital weights,
weighted mean, normal-approximation 5th-percentile lower bound, and
`clv_sample_size` = sum of event-capped weights. Unchanged code, new inputs.

A fill without a reference is excluded **with its reason**, never zero-filled:
`no_scheduled_end`, `near_scheduled_end`, `invalid_fill` or `no_reference`
(`post_entry_clv.py`). The power diagnostic (step 3) counts them per wallet. They
are not stored on the enrichment row, which would need a Postgres migration for a
diagnostic quantity. It also splits `no_reference` into not fetched, closed and
gap using the chunk receipts.

Point in time: a frozen generation reads entry prices with `observed_before` set
to its start, so rescoring a snapshot never sees prices fetched later.

## 3. Decisions for the owner

Chosen 2026-10-05: the recommended option in each case. D1: fills at least seven
days before the scheduled end. D2: resolved, exited and open bets. D3: against the
price paid. D4: keep 10 until the power diagnostic. D5: v5 redefines the `clv_*`
fields as 1 h post-entry CLV.

**D1. Which fills count.** *Recommended: only fills at least seven days before the
scheduled end*, the population the leakage bound was measured on. Without the
filter, the first report showed 16% of 1 h references at the outcome. The cost is
sample size: the filter keeps about 11% of resolved bets (1,670 of 14,452 in the
2026-10-05 08:10 report).

**D2. Which bets count.** *Recommended: resolved, exited and open alike.* A 1 h
reference needs no resolution; that is CLV's advantage (blueprint §5), and it adds
every exited and open bet to the sample. The pilot has not counted those yet, and
step 3 will. The leakage bound can only be checked on resolved bets.
The horizon report keeps checking it, and v5 adds no new leakage path, because the
reference is fixed 1 h after the fill. Conservative alternative: resolved and
exited only.

**D3. Spread.** *Recommended: measure against the price the wallet paid, as today.*
A buyer at the ask shows about half the spread as negative CLV before any edge. At
1 h that cost is the same size as the signal: in the deciding report, winners
averaged +0.3¢ and losers −1.1¢. The alternative, the mid at entry, measures market
drift rather than the wallet's execution, and it would loosen the gate (invariant
1). Expect this to block many wallets. That would be a result, not a defect.

**D4. `MIN_CLV_SAMPLE`.** *Recommended: keep 10 in v5 and re-decide only from the
power diagnostic in §4, step 3.* Blueprint open decision 2 already asks for this on
fresh data. A wallet whose 1 h CLV has mean μ and spread σ needs roughly
n_eff ≈ (1.645 σ / μ)² event-capped observations before its lower bound can clear
zero. With μ near 1¢ and σ of 5¢, an assumption step 3 replaces, that is about 70,
so many wallets may fail for lack of power rather than lack of edge. The diagnostic
measures that instead of assuming it.

**D5. Field semantics.** *Recommended: v5 redefines `clv_mean`,
`clv_lower_bound`, `clv_sample_size` and per-bet `clv` as 1 h post-entry CLV, and
drops closing-line CLV entirely* (it was leaky). The API and dashboard keep the
field names but relabel to "CLV (1 h after entry)" (`wallet.tsx`,
`SkilledBetsPanel.tsx`). Derived scores are disposable (invariant 2), so no
migration of old rows is needed.

## 4. Build steps

Each step is its own PR with its own evidence.

1. **Score input.** Add the `entry_prices/` store to `score_snapshot.INPUTS`, so
   the manifest inventories both files. Acceptance: inventory test. *Done:
   PR #60.*
2. **v5 scorer.** Implement §2 behind `score_version = "forecast-v5"`
   (`post_entry_clv.py`; `score_snapshot(score_version=...)`; the pilot's
   `score_version`, still `forecast-v4` when deployed), reusing `horizon_diagnostic`'s
   filter and `entry_prices.price_after`, and reading entry prices with
   `observed_before` set to the generation's start. Acceptance: unit tests on
   fixtures built through the real stores (the lesson of 2026-10-02: no invented
   fields). v4 output must be unchanged with or without entry prices, and a v5
   rescoring identical apart from `computed_at`.
3. **Power diagnostic (read-only).** Per wallet: n_eff, mean, SD and lower bound of
   1 h CLV; how many wallets could clear zero at their current mean and SD; the
   distribution of exclusion reasons. Published as
   `docs/benchmarks/<date>-gate13-power.md`. This is the evidence for D4.
   *Built as the pilot's daily `gate13` stage (`gate13_power.py`). It also scores
   v4 and v5 from the same inputs into a scratch directory, so its log line
   carries step 4's before/after counts. It cross-checks its per-wallet figures
   against the v5 generation. The benchmark write-up follows the first runs.*
4. **Cut-over with counts.** Switch the pilot's scorer to v5. Record gate-13 and
   overall tailable counts for v4 and v5 on the same frozen inputs in
   `docs/benchmarks/<date>-gate13-v5.md` (invariant 1). No threshold change in the
   same commit.
5. **Optional threshold change.** Only if step 3 shows `MIN_CLV_SAMPLE` measures
   collection rather than wallets, in its own commit with before/after counts and
   the owner's approval.

## 5. What to expect

- Few or no wallets pass gate 13 under v5. With D1 and D3 as recommended, the gate
  asks for confidently positive CLV net of the wallet's own spread, within an hour,
  on long-dated markets. That is a hard bar, and an empty result would be a
  finding (invariant 1), not a reason to loosen it.
- If step 3 shows almost no wallet can reach significance at any plausible edge,
  the honest options are to drop gate 13 from tailability and keep 1 h CLV as
  descriptive evidence, or to redesign it, for example as a prospective-only
  measure in Stage 3. That decision belongs to the owner and is recorded in the
  blueprint. The threshold is not tuned until wallets pass.
- The cohort is profit-seeded since 2026-10-03, so backward-looking v5 results are
  not evidence of skill (blueprint decision 6, seeding change). Steps 3 and 4
  describe the cohort; they do not validate the rule.

## 6. Timing

Stage 3 freezes model version and gate thresholds before the prospective window,
and allows no gate changes during it (blueprint §6, Stage 3). So v5, and any D4
outcome, must land **before** Stage 3 starts. Stage 3's entry gate is 14 days of
unattended pilot receipts with no unexplained gaps. The pilot's configuration
changed several times between 2026-10-01 and 2026-10-05, so count the 14 days from
the last change; Stage 3 then opens no earlier than about 2026-10-19. Steps 1–4 fit
before that.

## 7. Non-goals

- No change to h, the leakage bound, or the seven-day filter's definition.
- No ML, no new weighting scheme, no bootstrap in v5. Deterministic and
  explainable first (invariant 4).
- No use of pre-close prices in scoring.
