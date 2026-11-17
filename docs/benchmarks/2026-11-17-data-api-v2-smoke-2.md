# Data API v2 migration: second live smoke test (2026-11-17)

Second run of `scripts/smoke_data_api_v2.py` (unmodified) against the live API at commit
`4662fd5`, 2026-11-17 08:13:01–08:14:45 UTC, `--wallets 6`, compared against the last v1
research snapshot (`generated_at` 2026-11-02T15:47:45Z, `codex/research-snapshots` @
`d62fe7b`). Numbers are in `2026-11-17-data-api-v2-smoke-2.json`; paths below are into
that file. Counts, booleans, key names, HTTP statuses, request shapes and timings only.

**Not clean.** No part raised (`error_type` absent everywhere) and the live API refused
no request, but the collector's own 21-page bound fails the position listing of one of
the six pipeline wallets, on live holdings, not resolved ones (Unexpected 1).

The script made 268 requests to Polymarket, all HTTP 200, no 429/503, no `Retry-After`:
141 `/v2/positions`, 69 `/v2/activity`, 21 `/v2/leaderboard`, 12 `/v2/value`,
7 `/v2/user-volume`, 18 Gamma `/markets`. A follow-up probe added 53 (Unexpected 1).

## leaderboard

- `leaderboard.rows` 5; `leaderboard.keys` unchanged from the first run.
- `seed_entries` 120, `seed_names_present` 120, `seed_distinct_wallets` 119 (120 in the
  first run: one wallet now appears in two seed lists). Passes the criterion.

## v1_parity

- `wallets_compared` 10; every `per_wallet[*].finished` true.
- `totals.v1_rows` 988, `v2_rows` 988, `same_fill_count` 988, `v1_only` 0, `v2_only` 0.
- `single_fills_compared` 923; `size_equal`, `price_equal`, `notional_equal`,
  `outcome_equal` all 923.
- `multi_fill_keys` 32, `multi_fill_same_row_count` 32, `multi_fill_same_total_size` 32.

Clean, identical to the first run.

## resume

- `reference_rows` 1999, `resumed_distinct_rows` 1999, `resumed_walks` 11,
  `stalled_walks` 0, `errors` empty.
- `rows_missing_from_resumed` 0, `rows_only_in_resumed` 0,
  `rows_with_fewer_copies_in_resumed` 0.
- `distinct_store_keys` 1995, `rows_the_store_key_collapses` 4 (5 in the first run; not a
  failure criterion).

Clean.

## wallet_endpoints

Request: `GET /v2/positions?user=…&limit=500&filter_type=CASH&filter_amount=0.000001&sort_by=TOKENS&sort_direction=DESC[&cursor=…]`.

- `positions.walk_finished` true; `pages` 12 (11 × 500, then 241); `rows` 5741.
- `worth_nothing` 0; `null_current_size` 0; `sizes_non_increasing` true;
  `rows_served_twice` 0.
- `statuses`: OPEN 5738, REDEEMABLE 3. `redeemable_rows` 3, `below_one_share` 253,
  `live_holdings_kept` 5488, `live_statuses`: OPEN 5488 only, `parsed` 5488.
- `positions_through_runner`: `first` 5488, `second` 5488, `only_in_first` 0,
  `only_in_second` 0.
- Unrealized-PnL identity on the live holdings (`rows_with_every_number` 5488):
  `unrealized_is_value_minus_entry_cost` 5488 of 5488 **holds**;
  `unrealized_is_value_minus_size_times_avg_price` 4993 of 5488 **does not hold**
  (495 rows off).
- `closed_positions`: `rows` 50, `parsed` 50, `realized_pnl_non_increasing` true,
  statuses CLOSED 50. `value`, `volume`, `standing_all`, `standing_month`: present and
  numeric.

Clean for this wallet.

## pipeline

| | run 1 | run 2 |
|---|---|---|
| `seconds` | 22.5 | 17.9 |
| `hydration_wallets` | 6 | 6 |
| `wallet wallet=` log lines (`result.wallets_polled`) | 6 | 5 |
| of those, `errors=0` | 5 | 5 |
| sum of `combo_rows_skipped` | 8 | 23 |
| `positions_complete` | **5** | **5** |
| `closed_positions_complete` | 6 | 6 |
| `economic_all_time_complete` / `economic_month_complete` | 6 / 6 | 6 / 6 |
| `error_kinds` | `positions: positions pagination exceeded safety bound` ×1 | same ×1 |
| `result.positions` (rows written this run) | 13284 | 13284 |
| `position_rows` (file total) | 13284 | 26568 |
| `activity_rows_sharing_a_store_key` | 0 | 0 |
| `trades_without_condition` | 0 | 0 |
| `warning` | null | null |
| requests (all / `/v2/positions`) | 106 / 60 | 68 / 38 |
| `wallet_pnl_history_unavailable` lines | 6 | 5 |

`wallet_pnl_history_unavailable`: 11 lines, one per polled wallet per run, each after 4
attempts refused with `403 Forbidden` on CONNECT by this session's egress proxy
(`user-pnl-api.polymarket.com` is not reachable from here). Expected, not a finding:
Polymarket never saw those requests.

## snapshot

- `snapshot.status` `complete`, `publishable` true, `wallets` 6, 2.8 s.
- 13 requests (`/v2/leaderboard` ×1, `/v2/activity` ×6, `/v2/positions` ×6), all HTTP 200,
  `rows_rejected` 0 everywhere.

Clean.

## Compared with the first run

| | first run | second run |
|---|---|---|
| `wallet_endpoints.positions.rows` served | 20000 (harness cap) | 5741 |
| pages | 40 (not finished) | 12 (finished) |
| `worth_nothing` | not recorded | 0 |
| `redeemable_rows` | 18232 | 3 |
| `live_holdings_kept` | not recorded (OPEN 1768 in the first 20000) | 5488 |
| `rows_served_twice` | 1 | 0 |
| pipeline wallets | 4 | 6 |
| pipeline rows written per run (`result.positions`) | 139555 / 139557 | 13284 / 13284 |
| `/v2/positions` requests, whole script | 765 | 141 |
| `/v2/positions` requests per pipeline run | not counted | 60 / 38 |
| pipeline seconds per run | 50.2 / 51.1 | 22.5 / 17.9 |
| all requests, whole script | 874 | 268 |

## Unexpected

1. **`positions_complete` is 5 of 6 in both runs, with `error_kinds`
   `positions: positions pagination exceeded safety bound` ×1.** Cause is in the migrated
   code (`runner._paginate_positions`, 21-page bound), not in the smoke script and not a
   refusal by the API; the code was left unchanged.
   - Request shape, 21 times for one wallet, every one HTTP 200 with 500 rows and a
     further cursor: `GET /v2/positions?user=…&limit=500&filter_type=CASH&filter_amount=0.000001&sort_by=TOKENS&sort_direction=DESC[&cursor=…]`.
     The runner then raised; the wallet's log line reads `positions=0 errors=1`.
   - Follow-up probe, same request through `PolymarketClient.get_wallet_positions_page`,
     walked to the end: **53 pages** (52 × 500, then 10), 26010 rows, 0 served twice, all
     HTTP 200, 20.2 s. Statuses OPEN 25571, REDEEMABLE 439; `worth_nothing` 0;
     `null_current_size` 0; below one share 682; **live holdings kept 25232**.
   - So the server floor works (nothing worthless came back) and the listing is long
     because the wallet really holds about 25 thousand live positions of at least one
     share. The bound assumes a wallet's live holdings fit in 10500 rows; this wallet, one
     of the top six of the month's PnL leaderboard, needs 2.5 times that. Under the
     current bound its positions are never collected, on any run.
   - Run 2 selected 5 of the 6 watchlist wallets (`pipeline step=wallets watchlist=6
     selected=5`) and did not poll this one, yet still reports the same `error_kinds`
     entry and `positions_complete` 5 (carried from run 1's stored state). Why run 2's
     batch left it out was not established.
2. **`position_rows` per run is 13284 for 6 wallets, above the 10,000 line**, with the
   failing wallet contributing 0. The five wallets that completed hold 780, 5488, 6925,
   39 and 52 live positions. With the sixth collected in full the figure would be about
   38.5 thousand per run. The positions file again doubles on the second run (26568).
3. `seed_distinct_wallets` 119 against `seed_entries` 120 (see leaderboard). Within the
   criterion.
4. `resume.rows_the_store_key_collapses` 4. As in the first run, not a failure criterion.

Everything else on the list is clean: positions `walk_finished` true, `worth_nothing` 0,
`null_current_size` 0, `sizes_non_increasing` true, `rows_served_twice` 0, `live_statuses`
OPEN only, `parsed` equal to `live_holdings_kept`, `only_in_first`/`only_in_second` 0;
no `v1_only`/`v2_only` or value mismatch; `rows_missing_from_resumed` 0; `seed_entries`
120; snapshot `complete` and `publishable`; both pipeline runs under 120 s with a null
`warning`, no shared activity store keys and no trade without a condition.
