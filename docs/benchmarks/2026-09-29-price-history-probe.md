# Can closing prices be backfilled? Live probe of CLOB `/prices-history`

Run 2026-09-29 02:28–02:31 UTC on GitHub Actions
([run 36512775437](https://github.com/kevinmwpi/marketsignalos/actions/runs/36512775437)),
`scripts/probe_price_history.py` at commit `dc5ef64`. Read-only against public
endpoints. 57 markets: the top 6 by volume that closed in each year 2021–2025, those 6
plus 16 real markets from the 2026-09-12 metadata probe for 2026, and 5 still-open
markets. Full per-request results: [JSON](2026-09-29-price-history-probe.json).

## Why this was run

Closing-line value (CLV), the input to tailability gate 13, needs each market's price
shortly before it closed. The frozen 2026-06-12 snapshot held about 1.5 days of price
snapshots against 4.5 years of activity, so gate 13 failed 805 of 833 wallets, 632 of
them for too few observations. The question: is that history recoverable, or does it
have to be collected going forward?

A public report (py-clob-client issue 216, opened 2025-12-22, no maintainer reply,
repository archived May 2026) says resolved markets return nothing below 12-hour
granularity. That was tested rather than assumed.

## Result

Cells: markets returning at least one point / markets probed, with the median hours
between the last point at or before close and the close itself.

| Closed in | `interval=max`, 1 h | `interval=max`, 12 h | 14-day window, 1 h | 14-day window, 12 h |
|---|---:|---:|---:|---:|
| 2021 | 0/6 | 0/6 | 0/6 | 0/6 |
| 2022 | 0/6 | 1/6 | 0/6 | 0/6 |
| 2023 | 0/6 | 6/6 (5.6 h) | **6/6 (0.4 h)** | 6/6 (5.6 h) |
| 2024 | 0/6 | 6/6 (6.6 h) | **6/6 (0.1 h)** | 6/6 (6.6 h) |
| 2025 | 0/6 | 6/6 (7.5 h) | **6/6 (0.6 h)** | 6/6 (7.5 h) |
| 2026 | 0/22 | 22/22 (4.1 h) | **22/22 (0.1 h)** | 22/22 (4.1 h) |
| Still open | 5/5 | 5/5 | — | — |

"Window" means explicit `startTs`/`endTs` from 14 days before close to 1 day after.
Across all 40 markets closed in 2023 or later, the 1-hour window returned data for
every one; the last point landed at most **1.5 h** before close (90th percentile 0.81 h).
Every resolved market carried an actual `closedTime`.

## What this means

1. **The 12-hour floor is an artifact of `interval=max`, not of the data.** The same
   resolved markets that return nothing at 1 hour with `interval=max` return complete
   1-hour series when asked for an explicit time window. The public report tested
   only `interval=max`.
2. **Closing prices are recoverable at 1-hour resolution for every market that closed
   in 2023 or later,** typically within 10–40 minutes of close. Nothing about CLV is
   perishable: prices not collected since June can be fetched now.
3. **Markets from 2021–2022 have no order-book history.** Every request returned
   HTTP 200 with zero points, consistent with Polymarket's move from an automated
   market maker to its order book in late 2022. Bets on those markets cannot get a
   CLV and must be excluded with a recorded reason, not counted as missing data.
4. **A continuous price-snapshot collector is not needed for CLV.** It would store
   data that remains fetchable. The needed piece is a backfill of each resolved
   market's pre-close window.

## Limits

- Six markets per year, chosen by volume, plus 16 ordinary 2026 markets from the two
  metadata candidates. All 16 of those returned 1-hour windows too, but low-volume
  markets from 2023–2025 are untested and may be sparser.
- Price points are Polymarket's series values, not executable quotes; they carry no
  bid/ask or depth. Follower execution prices still have to come from quotes recorded
  at signal time.
- The endpoint is undocumented on retention. 2023 markets still return 1-hour data
  after about three years, but that is an observation, not a guarantee.
- For open markets the "hours before close" column in the JSON measures time until the
  scheduled end (e.g. 2028); it is not a gap in the data.
