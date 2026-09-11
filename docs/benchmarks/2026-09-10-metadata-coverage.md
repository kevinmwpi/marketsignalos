# Stage 1: metadata coverage diagnosis

Generated: 2026-09-11T04:33:05.159440+00:00. Frozen historical scores and market inputs; no new observations or model refit.

## Same-input coverage repair

| Measure | Stored | Recomputed |
|---|---:|---:|
| Metadata failures | 701 | 592 |
| Data-trusted wallets | 125 | 226 |
| Tailable wallets | 0 | 0 |

537 wallets have saved coverage counts/ratios that disagree with the scoring snapshot. All other qualification decisions were held fixed.

## Coverage decomposition

| Wallet-condition pair status | Count |
|---|---:|
| covered | 737950 |
| present_unsettled | 20352 |
| missing_market_rows | 208 |

## The two metadata-only candidates

| Wallet | Conditions | Stored covered | Recomputed covered | Present unsettled | Missing rows |
|---|---:|---:|---:|---:|---:|
| `0x521070c99db06e54af5e8e4a91d6858decdfbd53` | 21491 | 8364 | 20221 | 1234 | 36 |
| `0x5a218c7ad04135830a45c41aaed7294df7809318` | 17472 | 11905 | 15258 | 2209 | 5 |

## Limits

- Counts are distinct wallet-condition pairs, not unique global markets or trade rows.
- Present unsettled means an existing market fails the legacy coverage rule; it does not establish missing metadata or an invalid market.
- Absent records have unknown historical fetch cause. These inputs cannot distinguish never requested, empty Gamma response, HTTP error, or a capped/interrupted backfill.
- Category absence and positions pagination are not inputs to this coverage rule.
- The replay changes only metadata reasons; no numeric score, economic field, other rejection rule, or scoring threshold was changed.
- No currently qualified wallet or profitable follower strategy is established.

The JSON includes per-wallet counts, candidate missing condition IDs, full before/after waterfalls, and counterfactuals. Source and Parquet hashes were checked at both ends.

## Interpretation and remaining work

The confirmed repair is snapshot consistency: scoring now recomputes metadata
coverage from its own activity and market inputs without mutating saved hydration.
The 109 fewer metadata failures do not create a qualified wallet. Both candidates
still fail the unchanged settlement-proxy rule, with 41 distinct missing conditions
between them. Most remaining uncovered pairs already have market rows, so collecting
more rows alone does not resolve the coverage definition. Historical fetch failure
causes remain unknown without attempt receipts. A versioned separation of metadata
validity from resolution evidence, and bounded backfill receipts, are the next steps.

See [the runbook](../metadata-coverage.md) for reproduction, validation, and budget status.
