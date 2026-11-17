# Data API v1 `/positions`: what the default listing held (2026-11-17)

Question: this repository's collector called v1's `GET /positions?user=W` without a
`redeemable` parameter. Did that listing include holdings in resolved markets that
the wallet had not redeemed (`redeemable: true`), or leave them out? v1 was retired
on 2026-11-02 and can no longer be asked.

Fetched 2026-11-17 about 09:15–09:40 UTC with User-Agent
`MarketSignalOS-polymarket/0.1`. Quotes are verbatim; line breaks inside a quote are
the source's.

## Part 1 — documents

**Result: the documents are silent on v1's default.** No page still served describes
v1's `redeemable` or `sizeThreshold` parameters or their defaults. The v1 reference
has been removed, the old URLs redirect to the migration guide, and the API's own
v1 schema routes answer 410. What remains says only what v2 does, with one sentence
in the migration guide that can be read either way (see "Reading" below).

### 1. Migration guide

<https://docs.polymarket.com/migrate/data-api-v1-to-v2.md> — HTTP 200.

Route Mapping, the positions rows, whole:

> | `GET /positions` | `GET /v2/positions` | `status` defaults to open positions. `market` becomes `condition` |
> | `GET /closed-positions` | `GET /v2/positions?status=CLOSED` | Folded into the unified lifecycle |
> | `GET /v1/market-positions` | `GET /v2/positions?condition=…` | Market-anchored listing through the same route |

What Changed, the only paragraph that mentions positions, `redeemable` or
`mergeable`:

> **Position lifecycle.** v2 folds three v1 routes into one:
> `GET /v2/positions` serves the whole lifecycle behind a `status` filter
> (`OPEN`, `REDEEMABLE`, `REDEEMABLE_LOST`, `MERGEABLE`, `CLOSED`) with
> `redeemable` and `mergeable` flags on every row, replacing the separate
> `/closed-positions` and `/v1/market-positions` routes. `REDEEMABLE_LOST`
> narrows to still-held losing positions, and `MERGEABLE` to live complementary
> pairs a wallet can merge back to collateral.

The guide does not contain `sizeThreshold`, `size_threshold`, "resolved", "settled"
or "redeem" anywhere else. It lists the v1 parameters v2 rejects, and `redeemable` is
not among them:

> **Rejected v1 parameters.** v2 returns HTTP `400` for obsolete `market`,
> `market_id`, `marketId`, and `offset` parameters rather than ignoring them.

Its closing sentence on parameters:

> Each v2 endpoint page documents its full parameter set and row shape; several
> routes accept filters their v1 counterparts did not.

### 2. v2 positions reference

<https://docs.polymarket.com/api-reference/wallet/list-positions-for-a-user-or-market.md>
— HTTP 200.

> `status` defaults to `OPEN`, which is the **superset**: a
> settled-but-unredeemed winner still holds its tokens, so it is an open
> position whose condition resolved. `REDEEMABLE` narrows to that subset, and
> each row echoes its own `status`.

`status` parameter (same text in `/v2/openapi.json`):

> One of `OPEN`, `REDEEMABLE`, `REDEEMABLE_LOST`, `MERGEABLE`, or `CLOSED`;
> defaults to `OPEN`. […] `OPEN` is the superset; it includes
> settled-but-unredeemed winners, which `REDEEMABLE` narrows to. `CLOSED` is
> exited positions.

`filter_type` — the only place any page names the v1 size parameter:

> `CASH` or `TOKENS`; defaults to `TOKENS` (the /v2/trades-homogenized
> filter pair, replacing the former `size_threshold`).

`filter_amount`:

> The filter floor. `TOKENS`: minimum CURRENT holding in shares
> (defaults to the 0.1 dust floor; applies to `OPEN`/`REDEEMABLE`; a
> user's `CLOSED` set is not narrowed by it, and on a market anchor it
> moves the OPEN/CLOSED boundary).

Row field `status`:

> The row's actual state; can be narrower than the requested `status`,
> since an `OPEN` request also returns `REDEEMABLE` rows.

### 3. `/v2/openapi.json`

<https://data-api.polymarket.com/v2/openapi.json> — HTTP 200. The `status`
description is the one quoted above. There is no `redeemable` request parameter; the
route's parameters are `user`, `condition`, `limit`, `cursor`, `status`, `event_id`,
`title`, `filter_type`, `filter_amount`, `include_archived`, `sort_by`, `start`,
`end`, `sort_direction`. `Position.redeemable` (row field):

> Whether the position can be redeemed now: its market resolved and the
> tokens are still held (losing sides included; redeemable ≠ won).

### 4. Changelogs

<https://docs.polymarket.com/changelog/predictions.md> — HTTP 200 (403 on the first
attempt, see "Statuses"). Entries that mention positions together with redeem,
resolved, settled or a default:

**Nov 2, 2026**, "Data API v1 retirement":

> * **Data API v1 retires on November 2, 2026**: the legacy Data API reference has been removed. Follow the [v1 to v2 migration guide](https://docs.polymarket.com/migrate/data-api-v1-to-v2.md) for route mappings, response changes, and cursor pagination.

**Oct 7, 2026**, "Data API: /v2/tokens, plus the fixes shipped since September 28":

> * **Settled tokens priced at settlement**: on `/v2/positions` with a `condition` selector and on `/v2/oi`, a resolved outcome is valued at its settlement payout (0 or 1) before the last order-book mid, so a stale book on a settled market no longer moves current values or open interest.

**Sep 4, 2026**, "Data API v2 release":

> * **v1 status**: `/v1` keeps serving unchanged but is frozen. New fields and endpoints land on v2 only. The v1 reference lives under **Data API v1 (Legacy)**.

**Aug 10, 2026**, "Data API: per-outcome redemption activity, position fee basis
fields, and event artwork fallback" — the only entry about v1's `GET /positions`
defaults, and it concerns archived markets, not resolved ones:

> * **New `includeArchived` filter**: `GET /positions` accepts `includeArchived` to return positions in archived markets that are still active. Archived positions remain excluded by default, so existing clients are unaffected.

No entry mentions `redeemable` or `sizeThreshold` on v1's `/positions`.

<https://docs.polymarket.com/changelog/sdks.md> — HTTP 200 (403 on the first three
attempts). Entries carry version numbers, not dates. `0.10.0` is the release that
moved both SDKs to v2 (migration guide: "Data API v2 is supported starting with
version `0.10.0`").

TypeScript `0.10.0`:

> * Breaking change: `listPositions(...)` now covers open, redeemable, and closed positions. Replace `listClosedPositions(...)` with `status: PositionStatus.Closed`, and `listMarketPositions(...)` with a public client's `listPositions({ conditionId })`. Market-holder results are individual positions rather than groups by outcome. The default `Open` status includes redeemable positions.

Python `0.10.0`:

> * Breaking change: `list_positions(...)` replaces `list_closed_positions(...)` and `list_market_positions(...)`. Use `status="CLOSED"` for closed positions and `status="REDEEMABLE"` for redeemable positions. For a market's holders, use a public client's `list_positions(condition_id=condition_id)`. Secure clients default to the authenticated wallet. Market position results are individual `Position` rows.

TypeScript `0.12.0` / Python `0.12.0`:

> * Added `PositionStatus.RedeemableLost` and `PositionStatus.Mergeable` filters to `listPositions(...)`. […] Returned `Position.status` values remain `OPEN`, `REDEEMABLE`, or `CLOSED`, represented by `PositionRowStatus`.

Versions before `0.10.0` (the v1-backed SDK) have no entry that mentions `redeemable`
or a size threshold on positions.

### 5. Is any v1 reference still served?

| URL | HTTP | what came back |
|---|---|---|
| `https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user` | 200 after redirect; 403 on three later attempts | the migration guide (final URL `/migrate/data-api-v1-to-v2`) |
| `…/api-reference/core/get-current-positions-for-a-user.md` | 307 (403 on two other attempts) | `Location: /migrate/data-api-v1-to-v2.md` |
| `https://docs.polymarket.com/developers/misc-endpoints/data-api-get-positions` | 308 (403 on two other attempts) | `Location: /market-data/public-analytics` |
| `…/developers/misc-endpoints/data-api-get-positions.md` | 403 on all four attempts | `Forbidden` |
| `https://data-api.polymarket.com/openapi.json` | 410 | `{"error":"Data API v1 has been retired","migration":"https://data-api.polymarket.com/v2/","docs":"https://docs.polymarket.com/api-reference/data-api/overview"}` |
| `https://data-api.polymarket.com/v1/openapi.json` | 410 | same body |
| `https://data-api.polymarket.com/docs` | 410 | same body |
| `https://data-api.polymarket.com/swagger.json` | 410 | same body |
| `https://docs.polymarket.com/llms.txt` | 200 | 0 lines containing "redeemable" |
| `https://docs.polymarket.com/llms-full.txt` | 200 | 36 lines containing "redeemable", none about a v1 route; 0 lines containing `sizeThreshold` or `size_threshold` |

Statuses: the docs host answered 403 `Forbidden` (70 bytes) intermittently for URLs
that answered 200, 307 or 308 on another attempt, and 404 with a full page for a
made-up path. Its 403 is therefore throttling of this client, not "page absent". The
one URL that only ever answered 403 is unresolved; its extensionless twin redirects.
`/market-data/public-analytics.md`, the 308 target, answered 403 on the one attempt
and was not read.

The 36 "redeemable" lines in `llms-full.txt` are: token-redemption concept pages (6),
the migration-guide paragraph, the v2 positions reference (twice), the v2 overview
route table, SDK reference pages for the v2 `Position` type and combo positions, and
the SDK changelog entries quoted above.

### Reading

- **Stated:** nothing about v1's default.
- **Implied, weakly, in opposite directions:**
  - *For "v1 left them out":* the TypeScript SDK lists "The default `Open` status
    includes redeemable positions" inside a **breaking change** bullet. A sentence
    there is normally something that differs from before. The Python entry for the
    same release tells callers to "Use … `status="REDEEMABLE"` for redeemable
    positions", as if they previously needed a different call.
  - *For "v1 included them":* the migration guide's row for `GET /positions` says only
    "`status` defaults to open positions. `market` becomes `condition`", with no
    warning that the default set grew, although the guide is otherwise explicit about
    behaviour changes (rejected parameters, envelope, casing). And v2 has no way to
    exclude redeemable rows, which is an odd omission if v1 clients had been getting
    a live-only listing by default.
- Neither is evidence of v1's behaviour. The documents do not settle it.

## Part 2 — distribution

2026-11-17 09:46–09:48 UTC. 238 requests to `data-api.polymarket.com` (1 leaderboard,
237 position pages): 237 × HTTP 200, 1 × 503 retried successfully, no 429, no
`Retry-After`. No wallet failed. Counts only; numbers and the per-wallet count pairs
(unordered, no identifiers) are in `2026-11-17-data-api-v1-positions-default.json`.

**Request budget overrun.** A first attempt read the wallet address from the wrong
leaderboard key (`proxy_wallet`; the key is `user_id`), collected no wallets and kept
paging `/v2/leaderboard` for about 22 minutes (09:23–09:45 UTC) until it was noticed
and stopped. Those requests were not counted; at 0.2 s pause plus latency they are an
estimated 2,000–3,500, all to `/v2/leaderboard`, far above the 600 this task was
allowed. No position data came from that run. The corrected script caps its own
request count.

Wallets: the 100 rows of
`/v2/leaderboard?category=OVERALL&time_period=month&sort_by=PNL&limit=100`.

- **LIVE** — `/v2/positions?user=W&limit=500&filter_type=CASH&filter_amount=0.000001&sort_by=TOKENS&sort_direction=DESC`,
  at most 2 pages; rows with `redeemable: false` and `current_size >= 1`. Truncated
  (a third page existed): 11 wallets, counted as 1,000, a floor.
- **RESOLVED** — `/v2/positions?user=W&limit=1000&status=REDEEMABLE&filter_type=TOKENS&filter_amount=1`,
  at most 2 pages. Truncated: 17 wallets, counted as 2,000, a floor.

Means and maxima below are therefore floors wherever a group contains truncated
wallets; medians and quartiles are not affected except where noted.

### Row counts per wallet

| group | wallets | measure | min | Q1 | median | Q3 | mean | max |
|---|---|---|---|---|---|---|---|---|
| all 100 | 100 | LIVE | 0 | 0 | 16 | 221.5 | ≥186.4 | ≥1000 |
| | | RESOLVED | 0 | 1 | 53.5 | 794 | ≥516.6 | ≥2000 |
| | | LIVE+RESOLVED | 0 | 15.75 | 187.5 | 1071 | ≥703.1 | ≥3000 |
| LIVE < 200, LIVE not truncated | 74 | LIVE | 0 | 0 | 7 | 28 | 27.4 | 191 |
| | | RESOLVED | 0 | 1 | 34.5 | 574.5 | ≥390.0 | ≥2000 |
| | | LIVE+RESOLVED | 0 | 7 | 55.5 | 599 | ≥417.4 | ≥2191 |

Quartiles are inclusive-method (linear interpolation).

### Buckets (wallets)

| group | measure | = 0 | ≤ 10 | ≤ 50 | ≤ 200 | > 200 | truncated |
|---|---|---|---|---|---|---|---|
| all 100 | LIVE | 27 | 43 | 62 | 74 | 26 | 11 (> 1000) |
| | RESOLVED | 24 | 37 | 49 | 62 | 38 | 17 (> 2000) |
| | LIVE+RESOLVED | 7 | 21 | 36 | 52 | 48 | 24 |
| LIVE < 200 | LIVE | 27 | 43 | 62 | 74 | 0 | 0 |
| | RESOLVED | 18 | 31 | 41 | 52 | 22 | 8 (> 2000) |
| | LIVE+RESOLVED | 7 | 21 | 36 | 52 | 22 | 8 |

The ≤ columns are cumulative; "truncated" wallets are inside "> 200".

### Won and lost among RESOLVED rows

| group | RESOLVED rows counted | `current_value == 0` (lost) | `current_value > 0` (won, unredeemed) | wallets with ≥ 1 unredeemed winner |
|---|---|---|---|---|
| all 100 | 51,661 | 51,459 | 202 | 10 |
| LIVE < 200 | 28,859 | 28,740 | 119 | 9 |

99.6% of resolved, still-held rows are worthless losers.

### Which distribution fits "about 55 rows per wallet"?

If v1's default listed resolved holdings, a wallet's v1 row count was about
LIVE+RESOLVED; if not, about LIVE. For the 74-wallet proxy group today:

| | LIVE | LIVE+RESOLVED |
|---|---|---|
| mean | 27.4 | ≥ 417.4 |
| median | 7 | 55.5 |
| wallets above 200 rows | 0 of 74 | 22 of 74 (8 of them above 2,000) |
| chance that 28 wallets drawn from the 74 average ≤ 55 rows | — | 0.001% |
| …≤ 110 rows | — | 0.03% |
| …if every RESOLVED count is halved (stock was smaller before the midterms): ≤ 55 / ≤ 110 | — | 0.01% / 1.1% |
| …if quartered: ≤ 55 / ≤ 110 | — | 0.3% / 30% |
| chance that 28 wallets average ≥ 28 / ≥ 45 rows | 46% / 0.7% | — |

(200,000 draws of 28 without replacement, seed 17.)

Read plainly:

- **An average of 55 (or 45 across 64 wallets) is far below LIVE+RESOLVED as it
  stands today** for leaderboard wallets with modest live books: the mean is at least
  417 because 30% of them carry hundreds to thousands of dead rows. A v1 store built
  from a listing that included resolved holdings would be dominated by those wallets.
  It is not: the pilot's whole position store is 5,789 rows
  (`docs/lean-pilot.md`), fewer than three such wallets would write in one run.
- **It is above LIVE as it stands today** (mean 27, median 7), by about a factor of
  two. That direction is the expected one: these counts were taken two weeks after the
  US midterm markets resolved, which moved holdings out of LIVE. The first v2 run's
  3 rows per wallet sits at the low end of today's LIVE distribution (34 of 74 wallets
  hold ≤ 5), not outside it.
- So on averages the evidence favours "v1's default left resolved holdings out".

**But it is not decisive, for one reason that matters.** "LIVE < 200" is a poor
stand-in for the pilot's non-automated filter. The wallets that carry thousands of
dead rows are mostly high-frequency ones (several hold 0–15 live rows and > 2,000
resolved), which the pilot's style scoring would likely class as automated and never
poll. Remove them — take the 52 of 74 wallets with LIVE+RESOLVED ≤ 200 — and
LIVE+RESOLVED has mean 49.8 and median 18, against LIVE mean 18.8. That is as close
to 55 and 45 as anything here. Under that selection the "included" reading explains
both production numbers at once: 55 rows under v1 ≈ 3 live + about 50 dead, and 3
under v2. The "left out" reading instead needs the same wallets' live books to have
fallen from 55 to 3 across the midterms. Both are possible; this probe cannot tell
which wallets the pilot polled.

Other things that could produce 55 without settling the question: a few polled
wallets with large live books before the election pulling the mean up (the median
would then be much lower than 55); v1's `sizeThreshold` default of 1 against v2's 0.1
dust floor (irrelevant here, both sides were counted at ≥ 1 share); v1 rows for
markets closed but not yet resolved, which v2 may class differently (not measured);
and the resolved stock itself being smaller on any given October day than it is now.

## Answer

**Not settled.** Probably v1's default left unredeemed resolved holdings out — I put
it at roughly 60–65%, not higher.

For "left out":

1. v1 documented `redeemable` as defaulting to false, and an explicit
   `redeemable=false` did exclude them. A server that applies its documented default
   behaves the same with the parameter absent.
2. Polymarket's own TypeScript SDK changelog lists "The default `Open` status
   includes redeemable positions" under a breaking change in the release that moved
   to v2.
3. Averages: 55 rows per wallet is 2× today's LIVE mean and at most one-eighth of
   today's LIVE+RESOLVED mean for comparable leaderboard wallets.

For "included":

1. The migration guide, otherwise explicit about changed behaviour, maps
   `GET /positions` to `/v2/positions` with no warning that the default set grew.
2. A server that treats an absent boolean as "no filter" and a present one as a
   filter would show exactly what is known (explicit false excludes) and still include
   them by default. Nothing here rules that out.
3. Among wallets without a large dead book, LIVE+RESOLVED averages 50 — a better fit
   to 55 than LIVE's 19 — and matches the drop to 3 without needing the live books to
   have collapsed.

**What remains unknown:** v1's actual behaviour; which of today's leaderboard wallets
resemble the 28 the pilot polled; how much of each wallet's resolved stock existed
before 2 November; the content of the one docs URL that only answered 403 and of the
308 target `/market-data/public-analytics`.

**What would settle it, without the API.** The v1-era position store already holds
the answer. Each stored row keeps `size`, `current_outcome_price` and
`current_value_usdc` (`PolymarketPosition`), and 99.6% of resolved, still-held rows
are losers priced at exactly 0, while a live holding valued at exactly 0 is about 1 in
5,500 (`2026-11-17-data-api-v2-positions.md`). So, over snapshots taken before
2026-11-02:

- count rows with `size >= 1` and `current_outcome_price == 0` (or
  `current_value_usdc == 0`). A handful or none: v1 left resolved holdings out. A
  large share of all rows (tens of percent): v1 included them.
- cross-check with Gamma: stored rows whose market's resolution time is earlier than
  the row's `snapshot_at`.
- the per-wallet maximum and median of `position_count` in the snapshot manifests: a
  median far below the mean of 55, with a few wallets in the hundreds, is the
  signature of included dead books.

Until that is run, treat "the migrated collector stores what v1 stored" as likely but
unverified, and do not diff a v1-era snapshot against a v2-era one as if a missing key
meant an exit.

## Part 3 — what this repository's own records add

Added by the session that ran the migration, 2026-11-17 about 10:00 UTC. Counts only.

**The last v1 research capture** (`generated_at` 2026-11-02T15:47:45Z,
`codex/research-snapshots` @ `d62fe7b`). The collector then asked v1 for
`/positions?user=W&limit=50&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=0&redeemable=false`
and rejected any row whose `redeemable` was not `false`. Ten wallets from the day's
volume board: 350 rows returned, 0 rejected. Five listings were complete (2, 18, 20,
22 and 38 rows, under the limit of 50), and none of their rows has a reported value
or price of exactly 0. With `sizeThreshold=0`, a complete listing that included
resolved holdings would have carried the wallet's losers, which are valued at 0 and
which three in four leaderboard wallets hold today (Part 2). So an explicit
`redeemable=false` did filter in v1. The capture never asked without the parameter,
so it says nothing about the default.

**"v1 documented `redeemable` as defaulting to false"** (Answer, "For left out",
point 1) is the migrating session's recollection of the v1 reference, passed to the
helper in its brief. The reference has been removed and nothing in this repository
records it. Treat it as unverified. The same holds for v1's `sizeThreshold` default
of 1, which the collector's one-share floor is meant to match.

**The pilot's own runs.** The pilot asked v1 with neither `redeemable` nor
`sizeThreshold`.

- Before the outage: 1,488 to 1,660 position rows per run of 28 wallets (10-31 and
  11-01), or 53 to 59 per wallet, and 5,789 rows in the store for the two newest
  snapshots of each wallet that has any. That makes about 50 wallets with
  snapshots, not 64 (see the last item). No run was far from the others, so no
  wallet in the rotation listed more than a few hundred rows.
- First two runs on v2 (2026-11-17 09:07 and 10:07 UTC), live holdings of at least
  one share: 80 and 96 rows, each for a different batch of 28 wallets. Retention
  keeps two snapshots per wallet, so after each run it dropped the oldest snapshot
  of every wallet that had just written a third: 1,063 rows, then 369. A wallet whose
  listing was empty wrote nothing and dropped nothing. The wallets that still hold
  something therefore listed 1,063 rows on or before 2026-11-02 and at most 80
  today in the first batch, and 369 against at most 96 in the second.
- The cohort stage has excluded 101 wallets as automated and 64 remain. The pilot's
  rotation is the non-automated minority of what the leaderboard seeded. That is the
  caveat Part 2 raises about its 74-wallet group: the closer comparison is its 52
  wallets without a large dead book (LIVE mean 18.8; LIVE+RESOLVED mean 49.8,
  median 18).
- Seeds whose first listing was empty. A seed is polled again only if its first
  position listing stored something, and 12 to 14 of the 64 wallets appear never to
  have been polled a second time (`docs/lean-pilot.md`, known gaps). All of them
  were seeded on v1. If v1 listed resolved holdings, an empty listing meant holding
  nothing at all, which is 7 of today's 100 month-board wallets; if it left them
  out, it meant holding nothing live, which is 27 of 100. About a fifth of the
  pilot's seeds is nearer the second. This leans towards "left out", and rests on
  two things not established: that these wallets left the rotation because their
  listing was empty rather than because it failed (one did fail, on 2026-10-22), and
  that seeds in October resembled today's board.

**Where that leaves it.** Unsettled. The same wallets list far fewer rows than they
did, thirteen times fewer in one batch and about four times fewer in the other.
"Included" explains that as dead rows no longer listed; "left out" explains it as
live books that shrank across the 2026-11-03 elections, which wallets seeded from
October's profit board may have done. Neither reading predicts the difference
between the two batches. The share of seeds with an empty first listing leans
towards "left out", in the same direction as the 60–65% above.
(This paragraph first said "closer to even", on the first batch alone.)

The check under "What would settle it" is the one to run, and its input is leaving
the store. A wallet's v1 snapshots are dropped after its second non-empty listing on
v2, which for most wallets is within a day of the deploy. The v1 snapshots of wallets
whose v2 listing is empty stay, because an empty listing writes nothing, and those
are the informative ones: such a wallet either sold or redeemed everything since
2026-11-02, or its v1 rows were dead.

Nothing that is deployed depends on the answer. Scores, tiers and signal capture do
not read position rows (`docs/lean-pilot.md`).
