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

Pending: see the second commit.
