# Stage 3, cohort v1: plan for review

Status: **decisions chosen, build not started.** Written 2026-10-06. It turns
blueprint §6 Stage 3 and `docs/research-credibility.md` into decisions that must be
made, and frozen, before the prospective clock starts. Every number below is from the
pilot logs of 2026-10-05 and 2026-10-06; none is from an outcome.

**Chosen 2026-10-06, before any cohort-v1 outcome existed:** the recommended option in
each case.
- S1: nested tiers, with T2 (gates 1–12) primary.
- S3: event-weighted 1 h follower price improvement, net of measured costs (this
  answers blueprint open decision 4).
- S7: count the 14 days from the last Stage 3 pilot change.
- S2, S4, S5 and S6 as written.
- Disk: compress cold activity before the freeze and keep the 42-day cap.

## 1. What is settled

- **Question.** Does the frozen selection rule identify future positions that beat a
  price-only baseline after follower costs? (Blueprint §6 Stage 3.)
- **Freeze before looking.** `docs/evaluations/cohort-v1/frozen-config.json` is
  committed, and its hash recorded in the blueprint, before the window opens. No model
  or gate change during the window, and no peeking.
- **Entry gate.** Fourteen days of unattended pilot runs with no unexplained gaps,
  counted from the last pilot change. The last changes deployed on 2026-10-06 (PRs
  #65–#68). The Stage 3 build below changes the pilot again, so see S7.
- **Model.** `forecast-v5`, gate 13 on CLV 1 h after each buy (decision 6). The 13
  gates are listed in blueprint §2.
- **Bias, stated up front.** The cohort is seeded from the monthly profit
  leaderboard, so it is selected on recent winning. A null result can support the
  stop decision in §10; it cannot show that an unbiased cohort would fail.
- **Kill criterion 2.** If cohort v1 shows no edge over the price-only baseline after
  costs at the pre-declared date, stop and publish the null (blueprint §10).
- **Substrate.** The existing `signal_ledger`, extended with the cohort ID and the
  frozen config hash. No parallel mechanism. *Amended 2026-10-07 at build step 2:*
  the signal ledger is the API's record of feed signals. It takes tailable wallets only,
  keeps one row per position ever, and settles at resolution. Cohort v1 needs a row per
  signal, with the book and fee read when the pilot detects the buy, and the API does
  not run in the pilot. So capture writes `cohort-v1/signals.jsonl` in the pilot, with
  the cohort ID and config hash on every row. It is the only record of cohort-v1
  signals.

## 2. The problem: the rule qualifies no one

The 2026-10-06 gate-13 report scored 64 wallets:

| | wallets |
|---|---:|
| Tailable (all 13 gates) | **0** |
| Pass gates 1–12, blocked only by gate 13 | 6 |
| Pass gate 13 but fail an earlier gate | 3 |

The feed and the ledger take tailable wallets only, so a cohort frozen today would
record **no signals**. On the evaluation date there would be nothing to evaluate. That
is not a null result about edge: kill criterion 2 is undefined at n = 0. And waiting
until wallets qualify would mean choosing the start time by looking at the gates.

So the first decision is what the frozen rule is when its strictest form selects
nobody (S1). It has to be made now, before any outcome exists.

## 3. Decisions for the owner

**S1. The rule under test.** *Recommended: three nested tiers, frozen together; one is
primary.*

- T1: all 13 gates, the product rule. Expected to be empty or nearly so.
- T2: gates 1–12, with gate 13 recorded but not required. **Primary.** It is the
  smallest rule that still tests the model: lifetime fit, recency fit and economics.
  Six wallets today.
- T3: data gates 1–6 only, every screened wallet with complete data. Metadata
  coverage is complete for 29 of 64 wallets today, so at most 29. This is the
  leaderboard-selection baseline. It answers whether the model adds anything beyond
  "was on the profit leaderboard".

T1 is contained in T2, and T2 in T3.

One primary tier keeps one primary test. T1 and T3 are secondary and are reported
whatever they show. The alternatives are T1 alone, which is probably an empty
experiment, or postponing Stage 3, which means choosing the start by looking.

**S2. Cohort membership during the window.** *Recommended: freeze it at the cutoff.*
Every wallet screened at the cutoff, with its tier and its exclusion reasons, goes
into the frozen config. During the window:

- the cohort stage stops removing frozen members;
- refills and new seeds are collected but belong to cohort v2;
- T2 members, and an equal number of matched T3 non-members (S5), are polled every
  hour instead of about every 3.2 hours. Detection delay is then short and the same in
  both groups. If only T2 were polled hourly, a T2 lead over T3 could come from fresher
  data rather than the model.

Today 20 of 64 wallets are polled per run. Six T2 members plus six comparison wallets
polled hourly leave eight places a run for the rotation.
*2026-10-07:* the first provisional list had 16 members (8 T2, 8 comparison), which
would have left 4 places for 48 wallets, each polled about every 12 hours. The batch
is now 28, so 12 places refresh the rest about every 4 hours. Collection took about
3 minutes for 20 wallets, against 2,617 s used of the 5,400 s daily allowance
on 2026-10-06.

**S3. Primary outcome (open decision 4).** *Recommended: event-weighted signed price
improvement for the follower, 1 h after the follower's entry, net of costs.*

- Per signal: the price of the side bought, 1 h after the follower's entry, minus the
  follower's executable entry price (S4).
- Signals in the same event share one event weight, as gate 13 does.
- One hour matches the leakage bound that decision 6 measured. Almost every signal
  resolves within the window, so there is almost no censoring.
- Secondary outcomes: settlement ROI (censored if unresolved at the evaluation date),
  calibration, Brier score and log loss, and the 6 h horizon.

**S4. Follower cost model.** *Recommended: measured, never assumed.*

- **Entry time:** the first pilot run that sees the wallet's buy.
- **Entry price:** the best ask for the side bought, read from the order book in that
  run. The pilot does not record books for signals today; this is build step 2.
- **Fees:** the market's taker fee, read from Polymarket's API in the same run. The
  pilot stores no fees today. Which field carries the fee is to be verified in build
  step 2; neither zero nor a constant is assumed.
- **Excluded, with reasons, never zero-cost:** a signal with no book or no fee.
- **Size:** a $100 follower clip against the book depth, so slippage is measured
  rather than assumed.
- Wallet PnL and follower PnL stay separate.

**S5. Baseline and comparison.** *Recommended:*

- **Primary test:** T2 follower improvement after costs against zero, the price-only
  baseline: buying at the market price expects no improvement.
- **Secondary:** T2 against an equal number of T3 non-members, matched at the cutoff
  on main category and on a 30-day activity tercile, and polled on the same schedule
  (S2). Report the remaining imbalance.
- No per-wallet claims in v1, so no multiple-comparison correction across wallets.
  The three tiers are disclosed as three pre-declared looks at a nested rule.

**S6. Window length and evaluation date.** *Recommended: set from power before the
freeze, and capped.*

- In the two gate-13 reports, the median wallet's standard deviation of bet-level 1 h
  CLV was 2.7¢ and 1.1¢. Follower improvement is assumed to vary about as much.
- At σ = 2¢, detecting a +0.5¢ net improvement (one-sided 5%, 80% power) takes about
  100 independent events.
- Before the freeze, count T2's buys per day over the last 14 days. Set the window to
  the days needed for 100 events, at most 42 days.
- If 42 days cannot reach 100 events, say so in the frozen config. The test then
  stands as under-powered, and its null is reported as inconclusive, not as evidence
  of no edge.
- Analysis: event-cluster bootstrap, one look at the evaluation date.

**S7. When the window opens.** *Recommended: keep the 14-day rule and count from the
last Stage 3 pilot change.*

- Build steps 2–4 change what the pilot collects. The rule exists to show that the
  collection the test depends on runs unattended without gaps, so they count as
  changes.
- If they land by about 2026-10-13, the window opens about 2026-10-27.
- The alternative is to exempt additive capture code from the count. The window could
  then open on 2026-10-20, but the capture code itself would never have run
  unattended before the test.

## 4. Build steps (after the decisions)

Each step is its own PR. No step changes a score or a gate. Steps 0 and 2–4 change
the pilot, so they should land by about 2026-10-13 (S7).

0. **Compress cold activity** (the disk constraint, §5). Compress it the way PR #67
   compresses entry prices: every row is kept and reads are unchanged, verified by an
   identical v5 rescoring. *Built 2026-10-07 (`jsonl_archive.py`). Activity uses
   segments with an exactly-once commit, because unlike prices a repeated activity row
   would count a fill twice.*

1. **Tiers.** Compute T1/T2/T3 membership from a score generation. Write
   `frozen-config.json`: discovery source, cutoff, every screened wallet with its
   tier and reasons, score version, gate thresholds, code hash, outcome, horizon,
   cost model, baseline, matching rule, window and evaluation date.
   *Built 2026-10-07 (`cohort_v1.py`).*
   - It writes the frozen config and a separate member list, T2 plus its comparison
     set, which step 4 polls.
   - The member list is a data file on purpose. Step 4's code must run unattended
     for 14 days before the cutoff (S7), but the final members exist only at the
     cutoff. A provisional list from the current generation runs during the burn-in,
     and the final list replaces it at the freeze, with no code change.
   - The freeze reads the newest score generation that started at or before the
     cutoff, not the current one: a generation scored after the cutoff carries
     data the cutoff excludes.
2. **Signal capture in the pilot.** Record each new BUY by a frozen member: detection
   time, order-book top and depth for the side bought, and the market's fee. Append to
   the ledger with the cohort ID and config hash. Log counts only, never performance.
   *Built 2026-10-07 (`cohort_capture.py`, inside collection, before compaction).*
   - **Signal.** A member's BUY fills of one market and outcome, first seen by one
     collection. Only rows that collection appended are read, from a byte offset taken
     before it. Fills more than 3 h old when seen are counted, not recorded. That
     covers a wallet's first poll, or an outage longer than one skipped run.
   - **Book.** `GET clob /book` for the token bought. It records the best bid and ask
     and walks the asks for $100 of notional.
   - **Fee.** From `GET clob /clob-markets/{condition}`, field `fd` (rate `r`, exponent
     `e`). The fee is `shares × r × (p(1−p))^e` at each level, added on top, as
     Polymarket's own client computes it (clob-client-v2, `adjustBuyAmountForFees`).
     That client charges nothing when `fd` is absent, and capture does the same.
   - **Cross-checks.** Gamma's `feesEnabled` must agree with whether `r > 0`. Gamma's
     `clobTokenIds` must name the same token as CLOB, because activity rows carry only
     the outcome index, so a wrong token would price the other side.
   - **Exclusions.** A signal is written as excluded, with its reasons, when it has:
     no book or no asks; no fee details; fee or token sources that disagree; too thin
     a book for the clip; a market not accepting orders; or the 120 s capture cap
     reached.
   - **Failures.** A capture failure is reported in the collection result and never
     fails collection.
   - **Not verified live.** The field names are checked against Polymarket's client
     source, not against live responses, because this build environment cannot reach
     Polymarket. The burn-in is the check: the exclusion counts by reason are in
     every collection result.
   - **Finding: fees are material.** Polymarket's 2026 schedule charges takers
     r ≈ 0.03–0.07 on most categories; geopolitics is free. At p = 0.5 and r = 0.05
     that is 1.25¢ a share, 2.5 times the +0.5¢ net effect S6 is powered for. Fees
     move the mean, not the spread, so the power count stands. But a gross edge
     smaller than the fee shows as a null. So the frozen outcome adds a secondary: the
     same 1 h improvement before fees, to tell no edge from an edge the fees consume.
3. **Prices after the follower's entry.** Fetch hourly prices for at least one hour
   after detection for each recorded signal, reusing the entry-price backfill.
   *Built 2026-10-07 (`cohort_prices.py`, run first in the pilot's entry-price stage).*
   - **Window per signal.** It reuses the backfill's rules, not its 7-day chunks.
     Those are fetched only after they end, so signals in the window's last week
     would have no price until after the evaluation date.
   - **What is fetched.** The bought token's `/prices-history` from 1 h before
     detection to 6 h after, the secondary horizon. A window is fetched once it has
     ended and an hour has passed, so its prices are final. A failed window is retried
     an hour later.
   - **Which signals.** Every signal with a token, excluded ones included, so the
     evaluation can check that exclusions do not select on outcome.
   - **Fidelity.** It requests 5 minutes. Each receipt records the points returned
     and their median spacing. Before the freeze, check that the frozen tolerance
     (15 minutes before the horizon) fits the spacing the API actually returns.
4. **Freeze the membership.** Implement S2: cohort-stage exemption, hourly polling of
   members, new seeds tagged for v2.
   *Built 2026-10-07 as the pilot's `cohort_v1` stage, together with step 5's power
   count.*
   - It runs in provisional mode until `cohort_v1_freeze_at`.
   - At the first cycle after that time, it writes the frozen config once.
   - New seeds are cohort v2 by construction: they are absent from the frozen
     config.
   - *Fixed 2026-10-07:* on 08:1x the cohort stage excluded 6 systematic wallets, and
     at least one was a provisional member. Collection kept polling it first: it
     refetched the member's history (1,923 and 1,650 old fills at 09:07 and 10:07),
     only for the next cohort run to purge it again. Now an excluded wallet is
     neither polled nor tiered unless it is a frozen member, which the cohort stage
     never excludes. The fix changes what the pilot collects, so it restarts the
     14-day count (S7).
5. **Power count and freeze.** Count T2's events over the last 14 days and set the
   window (S6). Commit the frozen config, record its hash in the blueprint, and open
   the window.
6. **Evaluation (on the evaluation date only).** `docs/evaluations/cohort-v1/result.md`
   per blueprint §6 Stage 3 acceptance evidence.

## 5. Constraints and risks

- **Disk, the binding constraint.**
  - Growth: after compression (PR #67) the tracked stores grow roughly 65–70 MB a day,
    mostly activity (estimate from the evening runs of 2026-10-06).
  - Runway: about 4.2 GB was free at the last reading (2026-10-05), which lasts about
    8 weeks, to around 2026-12-01.
  - Conflict: a window opening on 2026-10-27 with the 42-day cap ends around
    2026-12-08.
  - Fix: before the freeze, either compress cold activity the way entry prices are
    compressed (the rows are raw evidence, so they are kept, not deleted), or cap the
    window at 28 days.
  - Score generations (about 7 MB a day) still have no retention (open decision 5).
  - *Measured 2026-10-07:* the first activity compaction moved 135.4 MB into one
    segment, and activity took 17.5 MB on disk afterwards, about 7.7 times smaller.
    Tracked storage fell to 195 MB from 292 MB. The runway now lasts well beyond a
    42-day window opening on 2026-10-27.
- **Runtime.** Hourly polling of about 9 members plus signal capture must fit the
  90-minute daily allowance. Measure it before the freeze. *2026-10-07:* 16
  provisional members and a 28-wallet batch. Read the day's runtime total from the
  receipts before the freeze.
- **T2 can change during the window.** Membership is frozen at the cutoff, so a member
  that later fails a gate stays in. That is intended: the test is of the rule at the
  cutoff.
- **Small cohort.** With six T2 wallets, a few wallets can dominate. Report each
  wallet's share of events alongside the cohort result. The horizon reports already
  show one wallet holding 45% of referenced bets.

## 6. Non-goals

- No change to the model, the gates, h, or the leakage rule. MIN_CLV_SAMPLE (D4) is
  decided, or left at 10, before the freeze.
- No public performance claim before the evaluation date.
- No automated trading; the follower is hypothetical (CLAUDE.md non-goals).
