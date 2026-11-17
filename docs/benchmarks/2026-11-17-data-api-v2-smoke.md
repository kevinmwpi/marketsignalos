# Data API v2 migration: live smoke test (2026-11-17)

One run of `scripts/smoke_data_api_v2.py` (unmodified) against the live API at commit
`264d498`, 2026-11-17 07:46:44–07:50:24 UTC, compared against the last v1 research
snapshot (`generated_at` 2026-11-02T15:47:45Z, `codex/research-snapshots` @ `d62fe7b`).
Numbers are in `2026-11-17-data-api-v2-smoke.json`; paths below are into that file.
Counts, booleans, key names and HTTP statuses only.

No part raised (`error_type` absent everywhere). The script made 874 requests to
Polymarket (all HTTP 200, no 429/503, no Retry-After): 765 `/v2/positions`, 60
`/v2/activity`, 17 `/v2/leaderboard`, 9 `/v2/value`, 5 `/v2/user-volume`, 18 Gamma
`/markets`. The hand checks added 8. That is above the ~800 budget; the cause is
position paging (see Unexpected 3).

## leaderboard

- `leaderboard.rows` 5; `leaderboard.keys`: `pnl`, `profile_image`, `rank`, `user_id`,
  `user_name`, `verified`, `volume`, `x_username`.
- `seed_entries` 120, `seed_distinct_wallets` 120, `seed_names_present` 120. Clean.

## v1_parity

- `v1_parity.wallets_compared` 10; every `per_wallet[*].finished` true.
- `totals.v1_rows` 988, `v2_rows` 988, `same_fill_count` 988, `v1_only` 0, `v2_only` 0.
- `single_fills_compared` 923; `size_equal`, `price_equal`, `notional_equal`,
  `outcome_equal` all 923.
- `multi_fill_keys` 32; `multi_fill_same_row_count` 32, `multi_fill_same_total_size` 32.
- `v1_rows_sharing_a_fill_key` 33 and `v2_rows_sharing_a_fill_key` 33 (equal).

Clean: v2 serves the v1 window fill for fill.

## resume

- `resume.reference_rows` 1999 (`reference_distinct_rows` 1999, `reference_exhausted` false).
- `resumed_walks` 11, `resumed_rows_read` 2012, `resumed_distinct_rows` 1999.
- `rows_missing_from_resumed` 0, `rows_only_in_resumed` 0,
  `rows_with_fewer_copies_in_resumed` 0, `stalled_walks` 0, `errors` [].
- `distinct_store_keys` 1994, `rows_the_store_key_collapses` 5 (see Unexpected 4).

Clean on every listed criterion.

## wallet_endpoints

Wallet: rank 1 of the month's PnL leaderboard.

- `positions.pages`: 40 pages of 500; `rows` 20000; `walk_finished` **false**.
- `rows_served_twice` **1**; `below_one_share` 0; `sizes_non_increasing` true;
  `null_current_size` 0; `parsed` 20000 (= rows).
- `statuses`: OPEN 1768, REDEEMABLE 18232; `redeemable_rows` 18232.
- Unrealized-PnL identity, against `rows_with_every_number` 20000:
  `unrealized_is_value_minus_entry_cost` **20000 / 20000**;
  `unrealized_is_value_minus_size_times_avg_price` 14360 / 20000. The entry-cost
  identity is the one that holds.
- `positions_ordering`: `by_size` 53656, `by_value` 19999, `only_by_size` **33657**,
  `only_by_value` 0.
- `closed_positions`: `rows` 50, `statuses` {CLOSED: 50}, `realized_pnl_non_increasing`
  true, `parsed` 50.
- `value.keys`: `proxy_wallet`, `value`; `value_is_number` true.
- `standing_all` and `standing_month`: `present` true, `pnl_is_number` true.
- `volume.keys`: `trade_count`, `volume`, `volume_usdc`; `volume_usdc_is_number` true.

## pipeline

Two runs, 4 wallets (`pipeline.runs[0]`, `runs[1]`).

| | run 1 | run 2 |
|---|---|---|
| `seconds` | 50.2 | 51.1 |
| `warning` | null | null |
| `error_kinds` | {} | {} |
| `hydration_wallets` | 4 | 4 |
| `positions_complete` / `closed_positions_complete` | 4 / 4 | 4 / 4 |
| `economic_all_time_complete` / `economic_month_complete` | 4 / 4 | 4 / 4 |
| `all_time_volume_positive` | 4 | 4 |
| `activity_history_complete` | 0 | 0 |
| `history_cursor_set` | 4 | 4 |
| `activity_rows` (file total) | 3935 | 7855 |
| `result.activity_records` (this run) | 3935 | 3920 |
| `activity_rows_sharing_a_store_key` | 0 | 0 |
| `trades_without_condition` | 0 | 0 |
| `activity_outcome_index_999` | 1126 | 2154 |
| `position_rows` (file total) | 139555 | 279112 |
| `closed_position_rows` | 200 | 200 |
| `gaps_file_present` | false | false |

The second run wrote 3920 new activity rows, so "no progress" does not apply even
though `activity_history_complete` and `history_cursor_set` are unchanged. Activity
types seen: TRADE, MERGE, CONVERSION, REDEEM, REWARD, YIELD, MAKER_REBATE,
TAKER_REBATE, and MIGRATION (2 rows, run 2 only). Clean on every listed criterion.

From the run log (not in the JSON): the runner logged `combo_rows_skipped` 19 for one
of the four wallets in run 2 and 0 for the others; Gamma backfill ended
`status=partial` (`requests=8 written=100 deferred_lookups=2288`), which is the
`market_pages=1` cap.

## snapshot

- `snapshot.status` `complete`; `wallets` 4; `publishable` true.
- 9 requests (`/v2/leaderboard` ×1, `/v2/activity` ×4, `/v2/positions` ×4), all
  `status` ok, `http_status` 200, `rows_rejected` 0.
- Positions samples: `rows_excluded` 34, 0, 31, 0 (of 50, 50, 50, 13 returned).

Clean.

## Hand checks

a. `GET /v2/positions?user=<wallet>&status=CLOSED&limit=50` twice, 20 s apart: HTTP 200
   both, 50 rows both. Row order **identical: yes**; 0 positions differ (by index and
   as a set of `condition_id`+`outcome_index`).

b. `GET /v2/activity?user=<wallet>&limit=500&sort_direction=DESC&start=1&exclude_deposits_withdrawals=true`:

| wallet | status | rows | rows carrying `is_combo` | `is_combo: true` |
|---|---|---|---|---|
| wallet_endpoints wallet | 200 | 500 | 0 | 0 |
| v1 snapshot wallet 1 | 200 | 500 | 0 | 0 |
| v1 snapshot wallet 2 | 200 | 500 | 0 | 0 |
| v1 snapshot wallet 3 | 200 | 500 | 0 | 0 |
| extra: the pipeline wallet with skipped combo rows | 200 | 500 | 4 | 4 |

   None of the four requested wallets has a combo row in its newest 500. Because the
   pipeline log showed skips for one wallet, that wallet was read too (one extra
   request). On its 4 combo rows:
   - Key names: `bio`, `condition_id`, `event_slug`, `icon`, `is_combo`, `name`,
     `outcome`, `outcome_index`, `price`, `profile_image`, `profile_image_optimized`,
     `proxy_wallet`, `pseudonym`, `side`, `size`, `slug`, `timestamp`, `title`,
     `token_id`, `transaction_hash`, `type`, `usdc_size`.
   - The only key a combo row has that other rows lack is `is_combo`; the key is
     **absent** (not `false`) on every non-combo row.
   - `condition_id` length **64** on combo rows, 66 on all non-combo rows of the page.
   - `type` values on combo rows: `TRADE`, `REDEEM`.

## Unexpected

1. **`wallet_endpoints.positions.walk_finished` is false.** This is the harness's own
   40-page cap, not the API stopping: the wallet holds 53656 positions at ≥1 share
   (`positions_ordering.by_size`, read to the end by `runner._paginate_positions`).
   Everything under `positions` therefore describes the first 20000 rows only.
2. **`positions_ordering.only_by_size` is 33657, `only_by_value` 0.** Not live change:
   the by-value walk hit the same 40-page cap (19999 distinct keys), so the difference
   is 53656 − 19999. Live changes between the two listings cannot be separated from the
   cap in this run; the only bound is `only_by_value` = 0 (nothing in the value listing
   was missing from the size listing). The two orderings were not compared in full.
3. **`wallet_endpoints.positions.rows_served_twice` is 1.** One (`condition_id`,
   `outcome_index`) key came back twice in the 20000-row `sort_by=TOKENS` cursor walk
   (also visible as `by_value` 19999 from 20000 rows in the default ordering). Not
   established whether this is a row moving between pages mid-walk or two rows sharing
   that key.
4. **Position volume.** Leaderboard-top wallets hold tens of thousands of ≥1-share
   positions, mostly `REDEEMABLE` (18232 of the first 20000 here). The four pipeline
   wallets wrote 139555 position rows per run and the positions file doubled on the
   second run (279112). 765 of this run's 874 requests were `/v2/positions`. Both
   pipeline runs still finished in about 50 s, under the 120 s line, but with 4 wallets.
5. **`resume.rows_the_store_key_collapses` is 5.** Of 1999 distinct activity rows, 1994
   distinct store keys: 5 rows that differ in content share
   (`transaction_hash`, `condition_id`, `outcome_index`, `type`) with another row and
   would be collapsed by the activity store. Not on the list of failure criteria;
   v1_parity shows the same shape (33 rows sharing a fill key on each side, equal totals).
6. **`user-pnl-api.polymarket.com` was not reachable from this session.** The migrated
   runner calls `GET https://user-pnl-api.polymarket.com/user-pnl` (parameters
   `user_address`, `interval`, `fidelity`) once per wallet, with 3 retries. All 32
   attempts (4 wallets × 2 runs × 4) got `403 Forbidden` on CONNECT from this session's
   egress proxy (host not on its allowlist), so **Polymarket never saw the request and
   this run says nothing about whether that endpoint still works.** The runner logged
   `wallet_pnl_history_unavailable` and carried on; `error_kinds` stayed empty. No
   error body `code`/`error`/`parameter` exists because the refusal was not Polymarket's.
7. **`is_combo` is present only when true**, and combo rows carry a 64-character
   `condition_id` against 66 elsewhere (hand check b). `runner._is_combo_row` tests
   `is True`, which matches this.
8. Request budget: 882 requests in total against a target of about 800 (see 4).

Nothing else was not clean: no migrated request was refused by the live API, and no
part reported an `error_type`.
