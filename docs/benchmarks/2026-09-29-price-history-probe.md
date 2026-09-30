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

## Backfill verified live

`closing_lines.py` was then run against the same 52 resolved markets on GitHub Actions
([run 36513669853](https://github.com/kevinmwpi/marketsignalos/actions/runs/36513669853),
commit `56b5f04`), twice into one store, and checked by `scripts/verify_closing_lines.py`:

| Closed in | Outcome |
|---|---|
| 2021 | `no_history` 6/6 |
| 2022 | `no_history` 6/6 |
| 2023–2026 | `ok` 40/40, last point at most 1.5 h before close |

The first run wrote 1,860 hourly observations (48-hour window). The second run skipped
all 52 markets as final and wrote nothing. Every request used `startTs`/`endTs` with
`fidelity=60`; none sent `interval`.

## Follow-up, 2026-09-30: the last pre-close price is the outcome

Before wiring these prices into CLV, the prices themselves were checked, using the
`last_before_close_price` this probe recorded (`window14d@60m`) for each of the 40
markets closed in 2023 or later:

| Distance of the last pre-close price from 0 or 1 | Markets |
|---|---:|
| ≤ 0.01 | 38 |
| 0.208 | 1 |
| 0.5 | 1 |

All 40 closed on an actual `closedTime`. By the time a market closes, its outcome is
almost always known, so the last price before close is effectively the result. CLV
against it (closing price minus entry) mostly restates whether the bet won, which
gates 8 and 9 already measure. Using it for gate 13 would turn the one gate meant as
independent evidence into a copy of the others, which amounts to loosening it
(blueprint invariant 1). Scoring therefore does not read the backfill.

A usable closing line has to come from before the outcome is known: a fixed lead
before close, or the price a fixed time after entry. Choosing between them is a
diagnostic for pilot data, which is why the worker collects each traded market's
48-hour pre-close window. Reproduce the table with:

```python
import json
d = json.load(open("docs/benchmarks/2026-09-29-price-history-probe.json"))
prices = [m["requests"]["window14d@60m"]["last_before_close_price"] for m in d["markets"]
          if m["group"].startswith("resolved-") and m["group"] >= "resolved-2023"]
print(len(prices), sum(min(p, 1 - p) <= 0.01 for p in prices))  # 40 38
```

The same flaw was latent in forecast-v4: its live snapshots of a market about to close
would carry the same near-final prices. It never showed because the frozen snapshot
held only about 1.5 days of snapshots.

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
