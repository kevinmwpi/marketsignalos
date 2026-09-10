# Stage 0: wallet qualification attrition

Generated: 2026-09-10T22:35:55.515644+00:00. Score version: `forecast-v4`.

Frozen snapshot SHA-256: `01564d095e7945cee89895f3f278a0f0d66e9aee6b88530325f27e10a447dc5f`.

833 wallets; 125 data-trusted; 0 tailable. No production settings changed.

## Cumulative waterfall and isolated failures

The cumulative columns apply blueprint gates 1–13 in order. Isolated counts use each stored gate decision across the full cohort, so they overlap. Gate 12 is conditionally evaluated; skipped wallets are not evidence of a passing recent edge.

| Gate | Requirement | Entering | Removed | Remaining | Isolated failures | Not evaluated | Only failure |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | Complete activity history | 833 | 11 | 822 | 11 | 0 | 0 |
| 2 | Complete current positions | 822 | 3 | 819 | 4 | 0 | 0 |
| 3 | Complete closed positions | 819 | 0 | 819 | 1 | 0 | 0 |
| 4 | Complete all-time economics | 819 | 1 | 818 | 2 | 0 | 0 |
| 5 | Complete 30-day economics | 818 | 12 | 806 | 17 | 0 | 0 |
| 6 | Complete market metadata | 806 | 681 | 125 | 701 | 0 | 2 |
| 7 | Independent settled events >=20 | 125 | 83 | 42 | 405 | 0 | 0 |
| 8 | Forecast confidence >=80% | 42 | 38 | 4 | 788 | 0 | 0 |
| 9 | Conservative forecast edge >0 | 4 | 1 | 3 | 799 | 0 | 0 |
| 10 | Positive all-time PnL/ROI and nonnegative 30-day PnL | 3 | 2 | 1 | 528 | 0 | 0 |
| 11 | Recent independent events >=5 | 1 | 0 | 1 | 302 | 0 | 0 |
| 12 | Recent forecast edge >=0 when gate 11 passes | 1 | 0 | 1 | 476 | 302 | 0 |
| 13 | CLV sample >=10 and lower bound >0 | 1 | 1 | 0 | 805 | 0 | 1 |

## Diagnostic counterfactuals

- Suspend data gates 1–6: **2** qualify.
- Suspend metadata gate 6 only: **2** qualify.
- Lower CLV sample from 10 to 5, keep positive lower bound and all other gates: **0–0** qualify (0 ambiguous at rounding boundaries).

## Failure overlap

| Group | Wallets |
|---|---:|
| coverage only | 2 |
| model or economics only | 125 |
| both | 706 |
| neither | 0 |

## Rounded score margins

These are descriptive percentiles, not uncertainty intervals or causal effects.

| Metric | Cohort | p10 | Median | p90 |
|---|---|---:|---:|---:|
| independent_settled_events | all_wallets | 0.0 | 22.0 | 1125.2 |
| forecast_skill_likelihood | all_wallets | 0.0928668 | 0.188951 | 0.3330892 |
| forecast_edge_lower_bound | all_wallets | -0.2729102 | -0.252458 | -0.1549494 |
| all_time_pnl_usdc | all_wallets | -9485.494 | -4.62 | 31835.306 |
| all_time_roi | all_wallets | -0.02007695 | -0.00010521 | 0.02373496 |
| pnl_30d_usdc | all_wallets | -1121.172 | -0.11 | 2875.65 |
| recent_independent_events | all_wallets | 0.0 | 16.112 | 939.74966 |
| recent_edge_mean | all_wallets | -0.1097764 | -0.088177 | -0.0453304 |
| clv_sample_size | all_wallets | 0.0 | 2.0 | 34.0 |
| clv_lower_bound | all_wallets | -0.1549214 | -0.008348 | 0.0008212 |
| independent_settled_events | trusted_wallets | 0.0 | 4.0 | 450.2 |
| forecast_skill_likelihood | trusted_wallets | 0.1393274 | 0.188951 | 0.2783482 |
| forecast_edge_lower_bound | trusted_wallets | -0.2607294 | -0.252662 | -0.2086478 |
| all_time_pnl_usdc | trusted_wallets | -5289.776 | -13.62 | 27828.068 |
| all_time_roi | trusted_wallets | -0.0141357 | -0.0005 | 0.03189029 |
| pnl_30d_usdc | trusted_wallets | -558.278 | 0.15 | 4963.524 |
| recent_independent_events | trusted_wallets | 0.0 | 3.4392 | 444.09958 |
| recent_edge_mean | trusted_wallets | -0.0974798 | -0.088177 | -0.0618212 |
| clv_sample_size | trusted_wallets | 0.0 | 0.0 | 1.0 |
| clv_lower_bound | trusted_wallets | -0.0016896 | 0.0 | 0.0 |

## Scope and verification

- Historical, selected wallet cohort; this is not a live feed or a representative census.
- Suspending data gates does not repair missing inputs or refit scores; it is not a causal estimate of what complete data would do.
- Baseline gates use stored pre-rounding rejection reasons. Gate 12 is not evaluated when gate 11 fails. Gate 13's confidence test is conditional on sufficient CLV sample.
- Margins use rounded serialized metrics. The CLV-5 counterfactual reports a range if rounding prevents an exact decision; its positive lower-bound requirement is retained.
- Forecast posterior confidence is model-dependent, not a calibrated population-wide false-discovery guarantee. No prospective, fee-adjusted copying profit is established.
- Raw activity and market files were hashed in the linked shadow benchmark, not rehashed by this diagnostic. This run verifies the derived score file and benchmark receipt.

Input hashes match both JSONL and Parquet shadow outputs. Wallet keys, score version, reasons, statuses, and compatibility with rounded metric intervals were checked. Pairwise overlaps and full failure patterns are in the companion JSON.

## Conclusion and next step

**(A), narrowly coverage-bound for two candidates; broad model/economic attrition
elsewhere.** Two wallets pass every model and economic rule and fail only metadata
completeness. Suspending all six data gates yields the same two; suspending metadata
alone does not turn the other 699 metadata failures into qualifiers. Of the 701
metadata failures, 673 also fail the positive conservative-edge gate. Across the
whole cohort, 770 of 833 fail at least four of gates 7-13. Median posterior skill
probability is 18.9%, and the median conservative edge is -0.252458 log-odds units.
Lowering CLV sample 10 to 5 still yields zero eligible wallets with no rounding
ambiguity. These are neither broad coverage-only failures nor evidence for a
blanket threshold change. Proceed to a bounded metadata-cause investigation of the
two candidates after branch review; do not infer that they are profitable to copy,
that repairing inputs would leave their scores unchanged, or that the wider
population has no edge. The observed snapshot is historical and selected, and
prospective performance after execution costs remains untested.

Reproduction and verification: [Stage 0 runbook](../gate-attrition.md).
The JSON contains measured results; this conclusion is a reviewed interpretation.
