# Polymarket Data API v2: docs and live probe, 2026-11-17

Evidence for the v1 to v2 migration of `polymarket_client.py`. Nothing here is
the migration itself.

- **Probe file:** `docs/benchmarks/2026-11-17-data-api-v2-probe.json`, written by
  `scripts/probe_data_api_v2.py` (unmodified) at 07:16–07:17 UTC: 117 GETs,
  statuses 200 ×97, 400 ×13, 410 ×6, 503 ×1. No 429.
- **Wallets:** `legacy` is `0x5668…5839` (inactive since 2024; raw rows kept).
  `active` is rank 1 by month PnL at probe time; its rows are reduced to
  shapes and counts. No price, size, value or PnL of the active wallet appears
  in either file.
- **Follow-up checks:** 42 further GETs made by hand after the probe, to settle
  questions the probe file left open. They are **not** in the JSON; each is
  marked "follow-up" and gives the request. They recorded statuses, counts and
  booleans only.
- JSON paths below are relative to the probe file's root. `{legacy,active}`
  means the finding holds at both paths.

Doc sources (fetched 2026-11-17):

| Key | URL |
|---|---|
| MIG | https://docs.polymarket.com/migrate/data-api-v1-to-v2.md |
| OV | https://docs.polymarket.com/api-reference/data-api/overview.md |
| ACT | https://docs.polymarket.com/api-reference/feeds/list-account-activity.md |
| POS | https://docs.polymarket.com/api-reference/wallet/list-positions-for-a-user-or-market.md |
| LB | https://docs.polymarket.com/api-reference/boards/get-the-trader-leaderboard.md |
| VAL | https://docs.polymarket.com/api-reference/wallet/get-portfolio-value.md |
| WA | https://docs.polymarket.com/trading/wallet-activity.md |
| RL | https://docs.polymarket.com/api-reference/rate-limits.md |
| CL | https://docs.polymarket.com/changelog/predictions.md |
| SDK | https://docs.polymarket.com/changelog/sdks.md |
| SPEC | https://data-api.polymarket.com/v2/openapi.json (linked from OV; the reference pages are generated from it) |

`https://docs.polymarket.com/api-reference/data-api/migrating-from-v1.md`
answers 307 to MIG; there is one migration page, not two.

---

## Part A — what the docs say

### a. Retirement

- "**Data API v1 retires on November 2, 2026.**" (MIG; CL entry of Nov 2, 2026
  adds "the legacy Data API reference has been removed").
- What v1 routes return after retirement: **not stated**. (Live answer in B1.)
- `GET /v1/accounting/snapshot` has no v2 counterpart (MIG).

### b. Route map

From the MIG "Route Mapping" table and "What Changed":

| v1 | v2 | Notes from the docs |
|---|---|---|
| `GET /activity` | `GET /v2/activity` | — |
| `GET /positions` | `GET /v2/positions` | "`status` defaults to open positions. `market` becomes `condition`" |
| `GET /closed-positions` | `GET /v2/positions?status=CLOSED` | "Folded into the unified lifecycle" |
| `GET /value` | `GET /v2/value` | — |
| `GET /v1/leaderboard` | `GET /v2/leaderboard` | — |
| `GET /trades` | `GET /v2/trades` | — |
| `GET /traded` | `GET /v2/user-stats` | "`data.trades` is the distinct-market count" |

Renamed or rejected parameters:

- `market` → `condition` (aliases `condition_id`, `conditionId`), at most 20
  comma-separated ids (MIG).
- "v2 returns HTTP `400` for obsolete `market`, `market_id`, `marketId`, and
  `offset` parameters rather than ignoring them." (MIG)
- "Request parameters accept both spellings on v2" (snake_case and camelCase; MIG).
- Positions: `filter_type`/`filter_amount` are "replacing the former
  `size_threshold`" (POS).
- Leaderboard: v2 names are `time_period`, `category`, `sort_by` (LB). The docs
  give **no** v1→v2 name map for the leaderboard (`timePeriod`, `orderBy`): not stated.

### c. Pagination

- Page size: `limit`. `/v2/activity`: "Page size; default 100, max 1000,
  past-cap rejected." (ACT). `/v2/positions`, `/v2/leaderboard`, `/v2/trades`:
  maximum 1000 in the schema; default **not stated** (SPEC).
- Cursor: `cursor`, taken from `pagination.next_cursor`. "Stop when
  `next_cursor` is `null`. `has_more` is exact, so an empty or short page does
  not mean the walk is over." (OV)
- Offset: "Pagination is cursor-only; there is no `offset` query parameter."
  (OV). v1 `offset` stopped at 10,000 rows (MIG).
- Pagination object, from the OV example: `limit`, `offset`, `has_more`,
  `next_cursor`. "The `offset` field in `pagination` is display metadata for
  numbering rows across pages, not a request parameter." (OV)
- "The `limit` parameter only applies to the first page; once a `cursor` is
  supplied, the cursor's own page size wins." (OV)
- Feeds: "re-send the same filters on every page. The cursor carries only its
  seek anchor, so changing a filter mid-walk silently re-anchors the feed." (OV)
- Positions: the cursor binds anchor, status, sort and direction;
  contradicting them is a 400; `title`, `condition`, `start`/`end` are not
  carried and must be re-sent (OV). A bare `?cursor=` on positions is a 400 (POS).
- Leaderboard: a cursor "pins the board it was minted on", so it can be
  followed with no other parameters (LB).

### d. Response changes

- Envelope: "Every v2 response wraps its payload in `data`, and paginated
  routes add a `pagination` object. A documented miss is `data: null` or an
  empty list, never an error." (MIG)
- Casing: "v2 responses are `snake_case` (`proxy_wallet`, `condition_id`)." (MIG)
- Units: "Bare `volume` and `size` values are outcome **shares**; fields
  suffixed `_usdc` are USD amounts". "All amounts are JSON numbers." (OV)
- Sentinels: "`outcome_index: 999` means the outcome could not be labeled. A
  missing or `null` numeric field means unavailable, never zero." (OV)
- Unit change between v1 and v2 for any HTTP field: **not stated**. (SDK says
  the *SDK* returns decimal strings and epoch **milliseconds**; that is the
  SDK's own layer, not the HTTP response.)
- **Activity rows**, v2 fields (SPEC `ActivityPage`, all required except
  `is_combo`): `proxy_wallet`, `timestamp` (epoch seconds), `condition_id`,
  `type`, `size` (shares), `usdc_size`, `transaction_hash`, `price`,
  `token_id`, `side`, `outcome_index`, `title`, `slug`, `icon`, `event_slug`,
  `outcome`, `name`, `pseudonym`, `bio`, `profile_image`,
  `profile_image_optimized`, `is_combo`.
  A per-field v1→v2 rename table for activity: **not stated**. The only doc
  evidence of v1 names is CL (Aug 10, 2026), which calls the v1 fields `asset`,
  `outcomeIndex`, `usdcSize`. So `asset` → `token_id` is an inference from
  that entry plus the v2 schema ("CLOB asset id of the outcome token"), and the
  rest is the mechanical camelCase → snake_case change.
- **Position rows**: SDK (Python) gives the renames `size` → `current_size`,
  `cur_price` → `current_price`, `total_bought` → `total_size`. SPEC adds
  `entry_cost_usdc` ("The fee-EXCLUSIVE entry basis"), `entry_fees_usdc`,
  `total_cost_usdc` (= the two summed), `unrealized_pnl`, `realized_pnl`,
  `total_pnl`, `status`, `first_entry_at`, `last_event_at`, `token_id`,
  `opposite_token_id`, `event_id`, `archived`. A full v1→v2 table
  (`initialValue`, `cashPnl`, `asset`, `oppositeAsset`, …): **not stated**.
  Note `percent_realized_pnl` is described as "A compatibility shape: despite
  the name, it is not `realized_pnl` over a basis" (SPEC).

### e. `/v2/activity`

- Default window: `start` "Omitted or `0` floors to three years back; pass
  `start=1` for full history." (ACT). `end`: "omitted or `0` means now plus
  one day" (SPEC).
- Default sort: "Ordering is `(block_timestamp, sequence_id)`, `DESC` by
  default or `ASC` via `sort_direction=ASC`". Only `sort_by=TIMESTAMP` is
  supported (SPEC).
- Bounds: `start` and `end` are both "epoch seconds (inclusive)" (SPEC).
- `exclude_deposits_withdrawals`: "Defaults to `true`, which drops `DEPOSIT`
  and `WITHDRAWAL` rows from the page, including when `type` names them." (ACT)
- Types in v2: `TRADE`, `SPLIT`, `MERGE`, `REDEEM`, `CONVERSION`, `MIGRATION`,
  `REWARD`, `YIELD`, `MAKER_REBATE`, `TAKER_REBATE`, `REFERRAL_REWARD`,
  `DEPOSIT`, `WITHDRAWAL`, `DIRECTIONAL_SPLIT`, `DIRECTIONAL_MERGE`,
  `THRESHOLD_COMPOSE`, `THRESHOLD_DECOMPOSE`, `TIP`. "Omitted, the feed returns
  every type except `TIP`." (SPEC)
- Which types v1 returned: **not stated** (the v1 reference was removed).
- `side`: "`BUY` or `SELL` on trade rows, from this wallet's perspective;
  empty where a side does not apply." (SPEC)
- `user` is required (SPEC).

### f. Combos

- Whether v1 `/activity` or `/positions` included combo rows: **not stated**.
  MIG lists separate v1 routes `/v1/activity/combos` and `/v1/positions/combos`
  mapping to `/v2/activity/combos` and `/v2/positions/combos`, which suggests
  but does not state that they were separate.
- v2 marking: `is_combo` is a "Flag only, on V2/V3 combo trade rows. Combo
  detail lives on the combos endpoints; omitted from non-combo rows." (SPEC)
- "For `TRADE`, `SPLIT`, `MERGE` and `REDEEM` rows, `is_combo=true` identifies
  Combo activity. Ordinary activity omits this field." (WA)
- On combo rows `outcome`/`outcome_index` are the basket token's Yes/No side,
  not a leg's (WA; CL Oct 7, 2026 says they were blank before).
- `/v2/value` includes "unresolved combo positions at cost basis" (VAL), and the
  leaderboard is "realized PnL, combos included" (LB).

### g. `/v2/positions`

- `status` omitted: "`status` defaults to `OPEN`, which is the **superset**: a
  settled-but-unredeemed winner still holds its tokens, so it is an open
  position whose condition resolved." (POS). Each row echoes its own `status`,
  which "can be narrower than the requested `status`, since an `OPEN` request
  also returns `REDEEMABLE` rows" (SPEC).
- One status per request: "One of `OPEN`, `REDEEMABLE`, `REDEEMABLE_LOST`,
  `MERGEABLE`, or `CLOSED`" (POS).
- v1 `sizeThreshold` (default 1): the v2 pair is `filter_type` (`CASH` or
  `TOKENS`, default `TOKENS`) and `filter_amount`: "`TOKENS`: minimum CURRENT
  holding in shares (defaults to the 0.1 dust floor; applies to
  `OPEN`/`REDEEMABLE`; a user's `CLOSED` set is not narrowed by it" (POS). v1's
  default of 1 is not mentioned; to keep it, send `filter_amount=1`.
- v1 `redeemable`: no request parameter of that name in v2. It maps to
  `status=REDEEMABLE` (narrowing) plus a `redeemable` flag on every row:
  "its market resolved and the tokens are still held (losing sides included;
  redeemable ≠ won)" (SPEC).
- `status=CLOSED`: "`CLOSED` is exited positions." `current_size` is "The
  CURRENT holding, in shares (~0 residual on the CLOSED arm)"; `total_size` is
  "LIFETIME bought shares (the WAC denominator), never the current balance"
  (SPEC). Default sort for `CLOSED` is `REALIZED_PNL`. How this differs from
  v1 `/closed-positions` row by row: **not stated**.
- Archived markets are excluded unless `include_archived=true`; "Inactive
  markets remain excluded either way." (POS)

### h. `/v2/leaderboard`

- `time_period`: "`day` | `week` | `month` | `all`. Defaults to `day`."
- `category`: "`overall` (default), a Gamma market category (e.g. `sports`)",
  or the synthetic `combos` and `esports`.
- `sort_by`: "`PNL` (default) or `VOLUME`." (all LB)
- Unit: "`volume` is in shares, not USDC." (LB)
- A wallet's USDC volume: `/v2/user-volume` returns
  `{ volume, volume_usdc, trade_count }`; "`volume` is both-sides SHARES;
  `volume_usdc` is the same measure in USD" (SPEC). Its bounds are floored to
  whole UTC days; omitted or `0` disables a bound (full history).
- `user=`: "one wallet's standing, carrying **both** the PnL and volume ranks
  in a single object rather than a page." Fields: `rank_pnl`, `rank_volume`
  (integer or null), `pnl`, `volume`, `user_id`, `user_name`, … (LB).
  `sort_by`, `limit`, `cursor` are ignored on that arm.
- **The docs contradict themselves here.** The route description says "A rank
  of `0` there means unranked for that board, not first", while the schema
  says `rank_pnl` is "`None` when unranked on the PnL board" (both LB).
- Semantics differ by window: "Finite windows (day/week/month) serve the
  MARKED equity change net of flows; they move with unrealized marks; `all` is
  the realized-only lifetime ledger." (LB)
- "tied users share a rank and the next one jumps… never compute a page from
  a rank." (LB)

### i. Rate limits

RL, Data API v2 (Cloudflare, per IP, sliding window; over-limit requests are
"throttled (delayed/queued) rather than immediately rejected"):

| Scope | Limit |
|---|---|
| General (all `/v2` endpoints) | 800 req / 10 s |
| `/v2/trades` | 300 req / 10 s |
| `/v2/positions`, `/v2/positions/combos` | 200 req / 10 s |
| `/v2/activity`, `/v2/activity/combos` | 200 req / 10 s |
| `/v2/prices-history` | 200 req / 10 s |
| `/v2/status` | 100 req / 10 s |

`/v2/leaderboard`, `/v2/value`, `/v2/user-volume`: no specific row, so the
general limit. Separately: "Heavy load answers with `429` and a `Retry-After`
header; retry after the given delay." (OV). SPEC also documents `503` with
`Retry-After` for a request-deadline or datastore timeout.

---

## Part B — what the live probe found

### 1. v1 routes now

All six (`/activity`, `/positions`, `/closed-positions`, `/value`,
`/v1/leaderboard`, `/trades`) answer **HTTP 410** with
`{"error": "Data API v1 has been retired", "docs": "…/api-reference/data-api/overview", "migration": "https://data-api.polymarket.com/v2/"}`.
Path: `v1_routes_now.*.{status,body}`.

### 2. Activity: envelope, paging, windows

| Question | Answer | Path |
|---|---|---|
| Top-level keys | `data`, `pagination` | `{legacy,active}.activity.default_first_page.top_level_keys` |
| Pagination object | `limit`, `offset` (0), `has_more`, `next_cursor` (opaque string, 179–195 chars) | `….default_first_page.pagination` |
| Newest first by default | Yes, across 3 × 500 rows | `….walk_desc_full_history.timestamps_non_increasing` = true |
| Default window vs full history | **Not discriminated.** Oldest row is identical with and without `start=1` (legacy 1728918952), because both wallets' histories start inside three years | `….default_window_oldest` vs `….full_history_oldest_timestamps` |
| `end` inclusive | Yes | `….window_bounds.end_is_inclusive` |
| `start` inclusive | Yes | `….window_bounds.start_is_inclusive`, `start_and_end_equal_all_at_mid` |
| Maximum `limit` | 1000 returns 1000 rows; 1001 is a 400 "limit must be between 0 and 1000" | `….limit_1000`, `….limit_1001` |
| Cursor with a different `limit` | Accepted, and the **cursor's page size wins**: cursor minted at `limit=5`, re-sent with `limit=7`, returned 5 rows | `….cursor_reuse.with_other_limit` |
| Cursor with a different filter | Accepted with 200, no error | `….cursor_reuse.with_other_filter` |
| `offset` | Rejected, 400, even with a valid request otherwise | `….offset_param` |
| Bad cursor | 400 "invalid cursor" | `….bad_cursor` |
| v1 `market` param | 400, names `condition` as the replacement | `….v1_style_market_param` |
| Missing `user` | 400 "required query param 'user' not provided" | `unknown_wallet.no_user` |
| Upper-case address | Accepted; rows come back with lower-case `proxy_wallet` | `unknown_wallet.uppercase_address` |
| Wallet with no activity | 200, `data: []`, `has_more: false`, `next_cursor: null` | `unknown_wallet.activity` |

Error bodies carry `code`, `error`, `retryable`, `trace_id`, and sometimes
`parameter` — more than the `{ "error": … }` shape OV shows.

### 3. Activity rows

Paths: `{legacy,active}.activity.walk_desc_full_history.shape_by_type.<TYPE>`.

- **Keys:** all 21 required keys are present on every row of every type
  (`absent` is `{}` for all types). `is_combo` did not appear on any of the
  3,000 rows walked, so nothing here tests combo rows.
- **Nulls:** no key was ever `null`. "Missing" is an **empty string**, plus
  `outcome_index` 999:

| Type | `side` | `outcome_index` | `outcome` | `token_id` | `condition_id` |
|---|---|---|---|---|---|
| `TRADE` | `BUY` / `SELL` | 0 or 1 | label | decimal string | 66-char hex |
| `REDEEM` | `""` | 0 or 1 | label | decimal string | 66-char hex |
| `MERGE`, `CONVERSION` | `""` | **999** | `""` | `""` | 66-char hex |
| `REWARD`, `YIELD`, `MAKER_REBATE`, `TAKER_REBATE` | `""` | **999** | `""` | `""` | **`""`** (also `title`, `slug`, `event_slug`, `icon` empty) |

- So yes: 999 is used for every row without an outcome. `REDEEM` rows do carry
  a real outcome index (per-outcome redemptions, as CL Aug 10 describes).
- **Types seen:** legacy `TRADE`, `REDEEM`, `MERGE`, `REWARD`; active adds
  `CONVERSION`, `YIELD`, `MAKER_REBATE`, `TAKER_REBATE`
  (`….walk_desc_full_history.types`). `SPLIT` was not seen for either wallet.
- **`side` values:** `"BUY"`, `"SELL"`, `""`.
- **Formats:** `transaction_hash` lower-case hex, 66 chars; `condition_id`
  lower-case hex, 66 chars (or empty, above); `proxy_wallet` lower-case hex,
  42 chars; `timestamp` is a JSON integer of 10 digits (epoch seconds);
  `price`, `size`, `usdc_size` are JSON floats on every row, including the
  rows where no price applies.
- Up to 8 (legacy) and 10 (active) rows share one second
  (`….max_rows_sharing_one_second`), so a `start`/`end` resume must rely on
  the inclusive bounds plus dedupe, not on timestamp uniqueness.

### 4. Dedupe-key collisions

The store's key is `(transaction_hash, condition_id, outcome_index, type)`.

| | legacy | active | Path |
|---|---|---|---|
| `dedupe_key_collisions` in 1,500 rows | 12 | 4 | `….walk_desc_full_history.dedupe_key_collisions` |
| `collision_examples_by_type` | `TRADE`: 12 | `TRADE`: 4 | same object |
| `identical_rows_repeated` | 0 | 0 | same object |

- Every collision is a `TRADE`. No row is ever repeated byte for byte, so the
  colliding rows are **distinct fills**, not duplicates.
- Legacy `collision_groups` (three groups of two rows each,
  `legacy.activity.walk_desc_full_history.collision_groups`): within a group
  the rows have the same `transaction_hash`, `condition_id`, `outcome_index`,
  `token_id`, `side` (`BUY`), `price` and `timestamp`. **They differ only in
  `size` and `usdc_size`.** Example: one transaction fills 231.19 and 5,768.81
  shares at 0.26; another 10,000 and 4,744.09 at 0.47.
- Consequence: the current key drops real fills. In the legacy sample that is
  12 of 1,479 trades (0.8%) by count, but the dropped rows are not small — in
  the first group above the larger fill is 96% of the transaction's size.

### 5. `exclude_deposits_withdrawals`

First 500 rows of full history (`{legacy,active}.activity.<name>.types`):

| Setting | legacy types | active types |
|---|---|---|
| `true` (`exclude_deposits_true`) | `TRADE`, `REDEEM`, `MERGE`, `REWARD` | `TRADE`, `REDEEM`, `MERGE`, `CONVERSION` |
| `false` (`exclude_deposits_false`) | the same plus `DEPOSIT` (54) and `WITHDRAWAL` (15) | the same plus `WITHDRAWAL` (1) |
| omitted (`walk_desc_full_history.types`) | no `DEPOSIT`/`WITHDRAWAL` in 1,500 rows | none in 1,500 rows |

Omitted behaves as `true`, as documented. With `false`, deposits and
withdrawals take up page slots: 69 of the legacy wallet's first 500 rows.

Type filters work: `type=TRADE` returns only trades; `type=TRADE,REDEEM`
returns both (`….type_trade`, `….type_trade_redeem`).

### 6. Positions

| Question | Answer | Path |
|---|---|---|
| Statuses when `status` omitted | Both `OPEN` and `REDEEMABLE` rows can come back. The burn wallet's default listing is 2 rows, both `REDEEMABLE` | `unknown_wallet.positions.shape.enum_values.status` |
| `default_vs_statuses` | Legacy: all empty (0 open, 0 redeemable). Active: default equals `OPEN` on the first page; `redeemable_inside_default` is 0 of 500 | `{legacy,active}.positions.default_vs_statuses` |
| Why 0 of 500 for the active wallet | First-page artefact. The default sort is `CURRENT_VALUE` descending and the probe compares one page of each listing | follow-up, below |
| Rows with `current_size` < 1 | Default/open first page: 0 of 500 (sorted by value, so not informative). `REDEEMABLE`: 12 of 500. `CLOSED`: 500 of 500 | `active.positions.<name>.current_size_below_one` |
| `filter_type=TOKENS&filter_amount=1` | No visible change on the first page (same 500 rows by enum counts) | `active.positions.tokens_at_least_one` vs `.default`; settled by follow-up |
| `status=OPEN,REDEEMABLE` | **Rejected**, 400 "invalid position status: OPEN,REDEEMABLE" | `{legacy,active}.positions.open_and_redeemable.error_body` |
| `status=REDEEMABLE_LOST`, `MERGEABLE` | Accepted; rows echo `REDEEMABLE` and `OPEN` respectively | `active.positions.{redeemable_lost,mergeable}.statuses_reported` |
| Keys, OPEN rows | 36 keys, all present on all 500 rows, none null | `active.positions.default.shape` |
| Keys, CLOSED rows | The same 36 keys, all present, none null | `{legacy,active}.positions.closed.shape` |
| CLOSED: `current_size` 0 and `total_size` positive? | **Not always 0.** Legacy: 21 of 22 rows are exactly 0, one is a non-zero residual below 1. Active: 384 of 500 exactly 0, 116 non-zero below 1. `total_size` is positive on the legacy rows (e.g. 13,175,255.227) | `….closed.current_size_zero`, `….closed.current_size_below_one`, `legacy.positions.closed.rows` |
| CLOSED rows flagged `redeemable: true` | Legacy 1 of 22; active 103 of 500 | `….closed.shape.enum_values.redeemable` |
| CLOSED row economics | On the legacy rows `entry_cost_usdc`, `total_cost_usdc`, `current_value`, `unrealized_pnl`, `percent_pnl` are all 0; `avg_price`, `realized_pnl`, `total_pnl`, `total_size` carry the history | `legacy.positions.closed.rows` |
| `include_archived=true` | Adds rows with `archived: true` (13 on the first page) | `active.positions.include_archived.shape.enum_values.archived` |
| Cursor walk | Two pages of 5, no repeated rows | `active.positions.cursor_walk_limit_5` |
| Empty book | 200, `data: []`, `next_cursor: null` | `legacy.positions.default` |

Formats: `condition_id` 66-char lower hex, `proxy_wallet` 42-char lower hex,
`token_id`/`opposite_token_id`/`event_id` decimal strings, `first_entry_at`
and `last_event_at` 10-digit integers (or `0`: both are `0` on the burn
wallet's rows, `unknown_wallet.positions.shape.keys`), all money and sizes
JSON floats, `end_date` a string.

**Follow-ups (not in the JSON), active wallet:**

- Took five conditions from `status=REDEEMABLE&limit=5`, then asked
  `/v2/positions?user=…&condition=<those five>` with `status` omitted and with
  `status=OPEN`. Both returned all five, each row `status: REDEEMABLE`,
  `redeemable: true`. So the default **is** the superset, as documented; the
  probe's 0-of-500 is the sort order.
- `sort_by=TOKENS&sort_direction=ASC&limit=50`: with no filter, all 50
  smallest holdings were in [0.1, 1); with `filter_amount=1`, all 50 were ≥ 1;
  with `filter_amount=0`, still none below 0.1. So the default floor is 0.1,
  `filter_amount=1` restores v1's threshold, and the 0.1 dust floor cannot be
  lowered.

### 7. Value

- Known wallet: `{"data": {"proxy_wallet": <42-char hex>, "value": <float>}}`,
  one object, not a list. Path: `{legacy,active}.scalars.value.data_object_shape`.
- Unknown wallet: the same object shape (not `data: null`, not an error).
  Path: `unknown_wallet.value.data_object_shape`.
- v1 returned a one-element array; v2 returns a bare object under `data`.

### 8. Leaderboard

| Question | Answer | Path |
|---|---|---|
| Canonical params | `category=OVERALL&time_period=month&sort_by=PNL` → 200 | `leaderboard.month_pnl` |
| v1 spellings | `timePeriod=MONTH&orderBy=PNL` → 200 | `leaderboard.v1_style_params` |
| Case | `time_period=MONTH` and `category=overall` → 200 | `leaderboard.uppercase_period`, `.lowercase_category` |
| Rejected | `sort_by=VOL` → 400 `{"code": "invalid_request", "error": "sort_by must be PNL or VOLUME", "parameter": "sort_by", "retryable": false, …}` | `leaderboard.v1_sort_name.error_body` |
| No params | 200 (docs: `day`, `overall`, `PNL`) | `leaderboard.no_params` |
| Row keys | `rank`, `user_id`, `user_name`, `pnl`, `volume`, `profile_image`, `x_username`, `verified`; no nulls, absent strings are `""` | `leaderboard.month_pnl.shape.keys` |
| `user_id` format | 42-char lower hex | same |
| Maximum `limit` | 1000 returns 1000; 1001 → 400 "limit must be between 0 and 1000" | `leaderboard.limit_1000`, `.limit_1001` |
| `paging_100_vs_2x50` | Identical: 100 rows each, same wallet order, 100 distinct wallets, ranks 1…100, second page 200 | `leaderboard.paging_100_vs_2x50` |
| Category | `POLITICS` works | `leaderboard.category_politics` |
| Standing, known wallet | `data` is one object: `user_id`, `user_name`, `pnl`, `volume`, `rank_pnl`, `rank_volume`, `profile_image`, `x_username`, `verified`; no `pagination` key | `{legacy,active}.scalars.standing_all` |
| Standing, wallet with no activity in the window | **`data: null`** (legacy wallet, `time_period=month`) | `legacy.scalars.standing_month.data_is_null` |
| Standing, unknown wallet | `data: null` | `unknown_wallet.standing.data_is_null` |
| Standing without `category` | Works | `{legacy,active}.scalars.standing_no_category` |
| `leaderboard_volume_unit` | standing `volume` ÷ user-volume shares = **1.0** for both wallets; ÷ user-volume USDC = 2.14 (legacy), 1.40 (active). The leaderboard's `volume` is shares | `{legacy,active}.scalars.leaderboard_volume_unit` |
| USDC volume | `/v2/user-volume` → `data: {volume, volume_usdc, trade_count}` | `{legacy,active}.scalars.user_volume_full_history.data_object_shape` |

**Follow-ups (not in the JSON):**

- `orderBy=VOLUME` and `sortBy=VOLUME` return the same top five as
  `sort_by=VOLUME`, and a different five from the PNL board; `orderBy=VOL` is
  a 400 naming `sort_by`. `timePeriod=MONTH` matches `time_period=month` and
  differs from the `day` default. So `orderBy` and `timePeriod` are real
  aliases, not ignored parameters — but the alias accepts only v2 **values**:
  v1's `VOL` must become `VOLUME`.
- `time_period=year` → 400 "time_period must be one of day, week, month, all".
- An unknown category (`NOT_A_CATEGORY`) → **200 with zero rows**, not an error.
- An unknown query parameter (`nonsense=1`) → 200, ignored.

### 9. Headers

- No `x-ratelimit-*`, `ratelimit-*`, `retry-after`, `deprecation`, `sunset` or
  `link` header on any 200 or 400 response. Path: every `headers` object, e.g.
  `{legacy,active}.activity.default_first_page.headers`.
- `cache-control`: `public, max-age=15` on activity, `max-age=5` on positions,
  `max-age=6` on value; none recorded on the leaderboard. A poll faster than
  those ages can be served a cached body.
- Follow-up: a full header listing of one v2 activity response showed
  `x-trace-id`, Cloudflare and CloudFront headers, and nothing about limits.
  The v1 410 carries no `deprecation`/`sunset` header either, only
  `cache-control: … no-store`.
- `retry-after: 1` does appear on the 503 below.

---

## Part C — surprises, and where docs and live API disagree

**Findings that should change the migration design**

1. **The dedupe key loses real trades** (B4). v2 returns one row per fill, and
   several fills in one transaction share
   `(transaction_hash, condition_id, outcome_index, type)`, differing only in
   `size`/`usdc_size`. Nothing else in the row distinguishes them — there is no
   log index or sequence id in the response, although the server orders by an
   internal `sequence_id`. Options: add `size` and `price` to the key (still
   collides on two equal fills in one transaction), or aggregate fills per key
   before storing. Whether v1 already behaved this way cannot be tested now;
   that it is not a v2 regression is an assumption until checked against
   stored v1 data.
2. **`side=` without `type=TRADE` times out on a busy wallet.** The probe's one
   503 is `active.activity.side_buy` (`code: request_timeout`,
   `retryable: true`). Follow-up: `side=BUY` and `side=SELL` on that wallet
   returned 503 after ~5.1 s on five of five attempts, with or without
   `start=1`, at `limit=100` or 500, each with `retry-after: 1`. Adding
   `type=TRADE` returned 500 rows in ~260 ms. The legacy wallet (mostly
   trades) did not time out (`legacy.activity.side_buy`). A retry loop will
   not fix this; the request shape has to change. Always send `type=TRADE`
   with `side`, or filter client-side.
3. **Absent values are `""` and 999, never `null` or a missing key** (B3).
   Reward and rebate rows have an empty `condition_id`. Any parser that treats
   `condition_id` as required hex, or `outcome_index` as a real index, needs a
   guard. Four reward-type rows in one transaction would also collide on the
   dedupe key (empty condition, 999, same type); none were observed.
4. **`status` takes exactly one value** (B6). `OPEN,REDEEMABLE` is a 400. The
   default already includes redeemable rows, so there is no need to combine;
   closed positions need a second request.
5. **CLOSED does not mean zero.** A quarter of the active wallet's first 500
   CLOSED rows have a non-zero residual below 1, and a fifth are flagged
   `redeemable: true`. `total_size` is lifetime bought shares, not the size at
   exit. Code that read v1 `/closed-positions` `size` as "shares held at
   close" has no equivalent field.
6. **Dust floor moved from 1 to 0.1** (B6 follow-up). Omitting the filter
   returns holdings in [0.1, 1) that v1's default hid. Send
   `filter_type=TOKENS&filter_amount=1` for parity.
7. **Leaderboard `volume` is shares** (B8), and the leaderboard default window
   is `day`. USDC volume needs one extra `/v2/user-volume` call per wallet.

**Docs versus live**

| Topic | Docs | Live |
|---|---|---|
| Error envelope | `{ "error": "…" }` (OV) | Adds `code`, `retryable`, `trace_id`, sometimes `parameter` |
| Unranked standing | Route text: rank `0` means unranked. Schema: `rank_*` is null when unranked (LB, self-contradictory) | Neither was observed: a wallet with no activity in the window returns **`data: null`** for the whole object. Ranks were positive integers when present |
| v1 after retirement | Not stated | 410 with a JSON body pointing at v2 |
| Leaderboard v1 parameter names | Not stated beyond "accept both spellings" | `timePeriod` and `orderBy` both work as aliases; `orderBy` is not the camelCase of `sort_by`, so this is an undocumented compatibility alias and should not be relied on |
| Unknown leaderboard category | Not stated | 200 with an empty list; a typo in a category name fails silently |
| Load shedding | "Heavy load answers with `429`" (OV) | No 429 in 159 requests at ~0.35–1.2 s spacing; the only failure mode seen was 503 `request_timeout` on one request shape |
| Rate-limit headers | Not stated | None sent |
| Changed filter with a cursor on a feed | "silently re-anchors" (OV) | Confirmed: 200, no error |
| Cursor page size | "the cursor's own page size wins" (OV) | Confirmed |
| Default `status` | `OPEN` is the superset (POS) | Confirmed by follow-up; the probe file's own comparison looks contradictory until the sort order is taken into account |
| `start`/`end` inclusive, `limit` max 1000, `exclude_deposits_withdrawals` default `true`, `offset` and `market` rejected | As stated | All confirmed |

**Not tested — do not treat as verified**

- **The three-year default window.** Neither probe wallet, nor any of the
  eight all-time top-volume wallets checked in follow-up, has activity older
  than 2023-11-18, so omitting `start` and sending `start=1` returned the same
  oldest row every time. The docs' claim is unverified live. Sending
  `start=1` is harmless either way and is the safe choice for backfill.
- **Combo rows.** `is_combo` never appeared, so its presence, type and effect
  on `condition_id` width are known from the docs only.
- **`SPLIT`, `MIGRATION`, `REFERRAL_REWARD`, `TIP` and the range-event types**
  were not observed.
- **429 behaviour and `Retry-After` on 429.**
- **A full walk to `next_cursor: null`** on a large history; the probe stops
  after two or three pages.
- **Parity with v1 values.** v1 is gone, so no field can be compared against
  what v1 returned for the same wallet. Stored v1 rows in the pilot's data
  directory are the only source for that comparison.
