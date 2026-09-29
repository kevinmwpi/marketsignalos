# Stage 0 follow-up: population-prior variance floor

Generated: 2026-09-28T22:44:49.828394+00:00. Score version: `forecast-v4`.

Stage 0 receipt SHA-256: `bbef4470a1f4673bf383df012cf687258b5333462c9553b64a28a9f708a9ed59`; frozen snapshot SHA-256: `01564d095e7945cee89895f3f278a0f0d66e9aee6b88530325f27e10a447dc5f`.

## Is the fitted prior at its floor?

A wallet with no settled evidence reports the prior point. Hypothesis: mu is the trusted-cohort median recent edge, -0.088177, and sigma² is the floor, 0.01.

| Prior-point metric at the floor | Predicted |
|---|---:|
| forecast_skill_likelihood | 0.188951 |
| forecast_edge_lower_bound | -0.252662 |
| recent_edge_mean | -0.088177 |

sigma² consistent with the rounded trusted medians: [0.00999983, 0.01000008]; floor inside: **True**. Stored quantiles equal to a prediction: all_wallets forecast_skill_likelihood median, all_wallets recent_edge_mean median, trusted_wallets forecast_skill_likelihood median, trusted_wallets forecast_edge_lower_bound median, trusted_wallets recent_edge_mean median.

## Can any prior produce a qualifier?

Only gates 8, 9 and 12 read the population prior. A wallet can qualify under some prior only if every gate it failed is one of those three.

| Other gates, as stored | Most qualifiers under any prior |
|---|---:|
| Production gates | **0** |
| Data gates 1–6 suspended | 14 |
| CLV gate 13 suspended | 13 |
| Data gates and gate 13 suspended | 154 |

Of the 42 data-trusted wallets with ESS ≥ 20, 42 fail gate 13. Their failures outside gates 8, 9 and 12:

| Prior-independent failed gates | Wallets |
|---|---:|
| 10, 13 | 29 |
| 13 | 13 |

Failures per wallet among model/economic gates 7, 10, 11 and 13, which no prior changes (all wallets):

| Prior-independent model/economic failures | Wallets |
|---:|---:|
| 0 | 14 |
| 1 | 151 |
| 2 | 311 |
| 3 | 161 |
| 4 | 196 |

## Estimator refit

**Pending local inputs.** The refit needs the frozen `enrichments.jsonl` and `bets.jsonl` from the verified enrichment shadow run; neither is in git. Run on the machine that holds them:

```powershell
.\.venv\Scripts\python.exe -m marketsignalos_polymarket.prior_floor `
  --attrition docs/benchmarks/2026-09-10-gate-attrition.json `
  --snapshot C:/path/to/enrichment-shadow/parquet/enrichments.jsonl `
  --bets C:/path/to/enrichment-shadow/parquet/bets.jsonl `
  --benchmark docs/benchmarks/2026-09-08-enrichment-shadow.json `
  --output benchmark-output/prior-floor/prior-floor.json
```

## Scope and verification

- The bound is exact for the stored decisions. Gates 7 and 11 sum event weights, gate 10 reads hydration economics, gate 13 reads closing-line value, and gates 1-6 read hydration, so none of them depends on the population prior.
- The refit rebuilds resolved bets from the bets export. Its entry price is the buys-only VWAP and its cost is net size times that price; both equal production's cost-basis inputs unless a position was bought again after a sell. The production-estimator reproduction check measures the effect before any variant is read.
- Variants are diagnostics, not proposed production estimators. Each applied variance keeps production's floor and cap, so the gates see what production would have seen.
- Historical, selected cohort. Qualifier counts are not prospective, fee-adjusted evidence that copying any wallet would be profitable.
## Mechanism (reviewed interpretation)

The production moment estimator subtracts each weak-prior fit's *posterior* variance
from the spread of the fitted means. For a separated fit, where every resolved bet
won or every one lost, no finite MLE exists. The N(0, 100) weak prior then holds the
mean near 1–2 log-odds while its posterior variance approaches the weak prior's own.
Ten 98-cent wins give a mean of 2.2 and a variance of 31. Such a fit subtracts far more
than it adds, so a few of them drive `total_var − within_var` negative and onto the
0.01 floor. Two parts of the original hypothesis do not hold:

- The ±20 edge clamp is unreachable under the weak prior. Two hundred winning 0.1-cent
  longshots reach only 14, so clamping both terms (variant c) should change nothing.
  The refit reports how many fits it would touch.
- ESS counts events, not information. Thirty 99.9-cent wins pass the ESS ≥ 20 filter
  (variant a) and still pin the estimate to the floor.

Precision-weighted DerSimonian–Laird and Paule–Mandel are insensitive to such fits.
The tests pin these behaviours in `test_prior_floor.py`. They are mechanism checks on
constructed fits, not measurements of this snapshot.

## Conclusion and next step

**The floor is real, but it does not explain the empty feed. It does bear on how many
coverage-bound candidates Stage 0 found.** The prior was fitted exactly at its floor
(σ² ∈ [0.0099998, 0.0100001], μ = −0.088177). The original 0.00998 came from pairing
two all-wallet medians that belong to different wallets; the trusted-cohort medians
pin it exactly. Every one of the 833 wallets fails at least one gate that no
population prior can change, so the qualifier count is **0 under every possible
prior**, and therefore under every estimator variant, without needing a refit. By
itself this cannot distinguish world (B) from (C): the count is zero by construction.
The prior does matter in two places. First, suspending data gates yields 2 wallets
under the floor but up to **14** under some prior. All 14 already pass gates 7, 10,
11 and 13, including confidently positive CLV on at least 10 observations, and are
blocked only by data gates and the prior-dependent gates 8, 9 and 12. So Stage 0's
"(A) for two candidates" is sensitive to the floor. Second, Stage 0's CLV-5
counterfactual examined only wallets failing gate 13 alone. That excluded the 11
data-trusted, ESS ≥ 20 wallets whose other failures are exactly 8, 9 and 12. The
"broad failure" reading survives the correction, although less strongly. Ignoring
gates 8, 9 and 12, 668 of 833 wallets still fail at least two model/economic gates
that no prior changes. The earlier "770 fail at least four of gates 7–13" counts
floor-driven gates three times. Gate 8/9 margins are set by the floor and should not
be cited as evidence of wide-margin failure. Also, 632 of 805 gate-13 failures are
too few CLV observations rather than negative CLV. **Next:** run the refit above on
the frozen `enrichments.jsonl` and `bets.jsonl`. Read the reproduction check first,
then the data-gates-suspended column under DerSimonian–Laird and Paule–Mandel. If
that count stays at 2, Stage 0's classification stands. If it rises toward 14, fix
the estimator before Stage 1 sizes its candidate list. That fix goes in its own
reviewed PR with this before/after evidence. It moves no threshold or floor, and
this diagnostic changes nothing in production.
