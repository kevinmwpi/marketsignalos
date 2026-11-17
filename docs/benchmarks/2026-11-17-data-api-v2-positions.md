# `/v2/positions`: leaving out resolved holdings (2026-11-17)

Live probe of `data-api.polymarket.com`, 2026-11-17 about 07:55–08:08 UTC. 248
requests (246 to the Data API, 2 for docs), all HTTP 200, no 429/503, no
`Retry-After`. Numbers are in `2026-11-17-data-api-v2-positions.json`. Counts,
booleans, key names, HTTP statuses, error text, request shapes and timings only.

Wallets, all from `/v2/leaderboard?category=OVERALL&time_period=month&sort_by=PNL`:
G = rank 1, S1 = rank 2, S2 = rank 6, S3 = rank 9 (ranks 3, 4, 5, 7, 8 did not end
within 7 pages). A row's key is (`condition_id`, `outcome_index`).

## Step 1 — docs

Sources: the list-positions reference page and `/v2/openapi.json` (same text in both).
The route's parameters are `user`, `condition`, `limit`, `cursor`, `status`,
`event_id`, `title`, `filter_type`, `filter_amount`, `include_archived`, `sort_by`,
`start`, `end`, `sort_direction`.

- `filter_type`: "`CASH` or `TOKENS`; defaults to `TOKENS` (the
  /v2/trades-homogenized filter pair, replacing the former `size_threshold`)."
- `filter_amount`: "The filter floor. `TOKENS`: minimum CURRENT holding in shares
  (defaults to the 0.1 dust floor; applies to `OPEN`/`REDEEMABLE`; a user's `CLOSED`
  set is not narrowed by it, and on a market anchor it moves the OPEN/CLOSED
  boundary). `CASH`: minimum mark-to-market `current_value` in USDC, on top of the
  token dust floor. Invalid values fall back to the default."
- Do the filters apply to REDEEMABLE rows? `TOKENS`: stated ("applies to
  `OPEN`/`REDEEMABLE`"). `CASH`: **not stated**.
- `start`: "Inclusive lower bound on `last_event_at`, epoch seconds; omit or `0` for
  unbounded (the `/v2/activity` + `/v2/trades` vocabulary)."
- `end`: "Inclusive upper bound on `last_event_at`, epoch seconds; omit or `0` for
  unbounded. A position with no native economics carries no `last_event_at` and is
  therefore **excluded by any bound**, in either direction; a window asks which
  positions moved inside it, and a row with no clock has no answer. Unbounded
  requests still serve those rows."
- `sort_by`: "One of `CURRENT_VALUE`, `PRICE`, `TOKENS`, `UNREALIZED_PNL`,
  `REALIZED_PNL`, `TOTAL_PNL`, or `TIMESTAMP` (the row's `last_event_at`). `PRICE`
  ranks by the response's effective `current_price` on every supported anchor and
  status. On `REDEEMABLE`, the existing winners-first partition remains ahead of the
  requested price order. The default follows the status: `CURRENT_VALUE` for
  `OPEN`/`REDEEMABLE`, `REALIZED_PNL` for `CLOSED`. …"
- A parameter that excludes resolved or redeemable holdings: **not stated**. There is
  none. `status` only narrows *to* `REDEEMABLE`, `REDEEMABLE_LOST` or `MERGEABLE`;
  `OPEN` "is the superset; it includes settled-but-unredeemed winners".

## Step 2 — reference set

`/v2/positions?user=W&limit=500&filter_type=TOKENS&filter_amount=1&sort_by=TOKENS&sort_direction=DESC`,
walked to the end. "Live" = `redeemable: false`.

| | G | S1 | S2 | S3 |
|---|---|---|---|---|
| pages | 108 | 3 | 1 | 2 |
| rows | 53667 | 1061 | 42 | 666 |
| `status` OPEN / REDEEMABLE | 5494 / 48173 | 51 / 1010 | 39 / 3 | 5 / 661 |
| `redeemable: true` | 48173 | 1010 | 3 | 661 |
| …of those, `current_value == 0` | 48173 | 1010 | 2 | 661 |
| live rows | 5494 | 51 | 39 | 5 |
| live, `current_value == 0` | 1 | 0 | 0 | 0 |
| live, `current_value < 0.001` | 16 | 0 | 0 | 0 |
| live, `current_price == 0` | 1 | 0 | 0 | 0 |
| keys served twice | 1 | 0 | 0 | 0 |

`redeemable: true` and `status: REDEEMABLE` coincide on every row. `current_size` was
non-increasing on all four walks; G's took 63 s (median 0.32 s per request).

G's 5494 live rows are 5493 distinct keys: see Answer, surprise 1.

## Step 3 — the value filter

`/v2/positions?user=W&limit=500&filter_type=CASH&filter_amount=A&sort_by=TOKENS&sort_direction=DESC`.
`A = 0.000001` was accepted (200) and did narrow, so 0.0001 was not tried. Every
request returned 200. On every walk: 0 rows with `current_value < A`, 0 with
`current_value == 0`, 0 with `current_size < 0.1`, `current_size` non-increasing.

| wallet, A | pages | rows | OPEN / REDEEMABLE | `current_size < 1` | keys twice | only in LIVE(W) | …value 0 | …0 < value < A | …neither | only in filtered |
|---|---|---|---|---|---|---|---|---|---|---|
| G, 0.000001 | 12 | 5750 | 5747 / 3 | 253 | 1 | 2 | 1 | 0 | 1 | 5 |
| G, 0.001 | 12 | 5685 | 5682 / 3 | 204 | 1 | 18 | 1 | 15 | 2 | 5 |
| G, 0.01 | 11 | 5087 | 5084 / 3 | 145 | 1 | 552 | 1 | 547 | 4 | 0 |
| S1, all three A | 1 | 51 | 51 / 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| S2, 0.000001 and 0.001 | 1 | 40 | 39 / 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| S2, 0.01 | 1 | 32 | 31 / 1 | 0 | 0 | 8 | 0 | 8 | 0 | 0 |
| S3, all three A | 1 | 7 | 5 / 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |

"Only in LIVE(W)" compares the filtered rows with `redeemable: false` and
`current_size >= 1` against the Step 2 live keys, classed by the Step 2 row's value.

Seconds per request (min / median / max): G 0.19–0.21 / 0.48–0.52 / 0.87–0.93;
S1 and S3 0.17–0.19 throughout; S2 4.81, 5.05 and 0.43 for its three single requests.

The "neither" and "only in filtered" rows on G are live change between the Step 2
walk and the filtered walks a few minutes later, not filter behaviour (follow-up,
one request per listing with `condition=` set to those markets):

- the 5 keys only in the filtered set: all 5 have `first_entry_at` after the Step 2
  walk began, and are now served by the unfiltered listing too;
- the 2 unexplained at A = 0.001 (one of them is the 1 at 0.000001): no longer
  served by any listing, including the default 0.1-share floor;
- the other 2 at A = 0.01: still held, now valued between 0.001 and 0.01.

So every live holding the filter drops is one whose value is below `A`.

The `REDEEMABLE` rows that survive (G 3, S2 1, S3 2) all have `current_price == 1`
on G: unredeemed winners. The filter removes only the worthless losing side.

## Step 4 — other candidates (first page, `limit=500`, G and S1)

| request | G | S1 |
|---|---|---|
| plain (Step 2 shape) | 200; OPEN 31, REDEEMABLE 469 | 200; OPEN 24, REDEEMABLE 476 |
| a. `+redeemable=false` | 200; same keys, same order as plain | 200; same keys, same order |
| b. `+redeemable=false&status=OPEN` | 200; same keys, same order as plain | 200; same keys, same order |
| c. `filter_type=CASH&filter_amount=0.001&sort_by=CURRENT_VALUE&sort_direction=DESC` | 200; OPEN 500, REDEEMABLE 0; `current_value` non-increasing | 200; 52 rows, all OPEN; non-increasing; no second page |
| e. `filter_type=CASH&filter_amount=0.001`, no `sort_by` | 200; OPEN 500; `current_value` non-increasing, `current_size` not | 200; 52 rows, all OPEN; same order as c |

- a, b: `redeemable=false` is accepted and **silently ignored**; `status=OPEN` is the
  documented superset. Neither removes a single row.
- c on G, two pages of 500 by cursor against one `limit=1000` request, run twice.
  First run: 1 key repeated across the two pages, 1 key of the `limit=1000` response
  missing from them, 1 extra. Second run, keyed on `token_id` as well: 1
  (key, `token_id`) row served on both pages, 2 rows of the `limit=1000` response
  missing, 1 extra, order not identical. The value ordering moves with live prices
  between requests, so a value-sorted cursor walk repeats and skips rows.
- e: the default order is `CURRENT_VALUE` descending, as documented; on G its first
  page was not identical to c's, for the same reason.
- c and e on G took 1.8–2.0 s per request against about 0.5 s for the `TOKENS` sort.

## Step 5 — cursor stability

G, `filter_type=CASH&filter_amount=0.001&sort_by=TOKENS&sort_direction=DESC&limit=500`,
two full walks 30 s apart.

| | walk 1 | walk 2 |
|---|---|---|
| pages | 12 | 12 |
| rows | 5685 | 5683 |
| OPEN / REDEEMABLE | 5682 / 3 | 5680 / 3 |
| keys served twice | 1 | 1 |
| (key, `token_id`) served twice | 0 | 0 |
| `current_size` non-increasing | true | true |

Keys only in walk 1: 2. Keys only in walk 2: 0. Of the keys in both, 1 changed
`current_size`. Checked afterwards: neither of the 2 is served by the CASH listing
any more and both are valued below 0.001 (one is still at ≥ 1 share, the other is
now below 1 share). Those are real changes in the book, not cursor faults. The one
key served twice in each walk is the same two-row key as in Step 2.

## Answer

**Request.**

```
GET /v2/positions?user=W&limit=500&filter_type=CASH&filter_amount=0.001&sort_by=TOKENS&sort_direction=DESC
```

followed by `pagination.next_cursor` until it is null, then drop client-side the rows
with `redeemable: true` and the rows with `current_size < 1`. Both client filters are
needed: the request still returns unredeemed winners (0–3 rows per wallet here) and,
because only one `filter_type` can be sent, holdings between 0.1 and 1 share (G 204,
S3 2, S1 and S2 0).

**What it loses.** Live holdings worth less than 0.001 USDC: G 16 of 5493 (1 at value
exactly 0, 15 between 0 and 0.001); S1, S2, S3 none. Nothing else: every other
difference was a position opened or exited between walks.

`filter_amount=0.000001` is accepted and costs the same 12 pages on G (5750 rows
against 5685). It loses only the 1 holding whose value is exactly 0. That one is
unreachable at any floor: it is the live row with `current_price == 0`.

**Cost for G.** 12 requests, about 0.5 s each at the median, against 108 for the
unfiltered listing. S1 goes from 3 requests to 1, S3 from 2 to 1, S2 stays at 1.

**Surprises.**

1. **(`condition_id`, `outcome_index`) is not unique.** G holds two live rows that
   share that pair and differ in `token_id` (and in size, cost and timestamps). They
   came back in every listing of G, 7 rows apart, and no (key, `token_id`) pair was
   ever served twice under the `TOKENS` sort. The "row served twice" in the smoke
   test is this, not a cursor fault. A store keyed on the pair keeps one of the two.
2. **A value floor makes a held position disappear when its price falls**, and
   reappear if it recovers. Two keys left the listing in 30 s in Step 5 without the
   position being exited in at least one case. Anything that diffs consecutive
   snapshots and reads an absent key as "closed" will see these. The lower floor
   (0.000001) shrinks the set to holdings priced at exactly 0.
3. `redeemable=false` returns 200 and changes nothing. A v1 request shape carried over
   unchanged would not fail; it would silently read the full book.
4. The `CASH` filter does remove `REDEEMABLE` rows (the docs do not say whether it
   applies to them): all 48173 of G's had value 0, and 3 remained.
5. `sort_by=CURRENT_VALUE`, which is also the default, is not safe to page: it
   repeated and skipped rows between two requests seconds apart and was about four
   times slower per request on G.
6. Two of S2's three single-page requests took about 5 s; the third, and every other
   wallet's, took under 1 s. Not explained.

Not measured: whether a holding in a market that has closed but not yet resolved is
served as `OPEN`, `start`/`end` or `sort_by=TIMESTAMP` as a way to read only what
changed, and cursor stability at `filter_amount=0.000001` (Step 5 ran at 0.001; the
sort and cursor kind are the same).
