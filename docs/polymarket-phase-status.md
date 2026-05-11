# Polymarket Pipeline Phase Status

This note tracks completion against the pivot plan for the Polymarket-driven
skill ranking and cross-exchange signal pipeline.

## Phase 0 - Discovery

Status: mostly complete.

Completed:

- Validated public Polymarket data surfaces across Gamma, CLOB, Data API,
  leaderboard API, and Goldsky.
- Confirmed read endpoints used by the first ingestor pass are unauthenticated.
- Confirmed `lb-api.polymarket.com/profit?window=all` returns top wallet rows
  with `proxyWallet`, amount, pseudonym, and name.
- Confirmed `data-api.polymarket.com/activity`, `trades`, `positions`, and
  `value` expose wallet-level activity and holdings.
- Confirmed `gamma-api.polymarket.com/markets` returns open and closed market
  metadata with condition IDs, questions, categories, outcome prices, and dates.
- Confirmed the Goldsky subgraph is reachable and can return order fill events.
- Added `scripts/probe_polymarket.py` to reproduce endpoint probing and refresh
  `docs/polymarket-api-discovery.md`.

Remaining:

- Confirm accepted volume leaderboard windows and whether `window=all` is enough
  for the initial volume-ranked seed list.
- Validate Data API pagination for large wallet backfills.
- Expand Goldsky probes into full-history queries if Data API pagination proves
  incomplete.
- Run a full manual win/loss probe joining one wallet's activity to resolved
  Gamma markets.

## Phase 1 - Polymarket Ingestor Scaffold

Status: substantially complete.

Completed:

- Added `services/polymarket-ingestor` as a separate Python package.
- Registered the `marketsignalos-polymarket-ingestor` CLI entry point.
- Added a synchronous HTTP client with retry handling for leaderboard, wallet
  activity, wallet positions, wallet value, and Gamma market metadata.
- Added typed dataclasses for leaderboard entries, wallet activity, positions,
  market records, and wallet value snapshots.
- Added JSONL stores for leaderboard, activity, positions, markets, and wallet
  values, including dedupe indexes for unbounded activity and market streams.
- Added Postgres store stubs for the later operationalization phase.
- Added CLI modes for `leaderboard`, `wallets`, `markets`, and `all`.
- Added tests for client behavior, parsing, storage round trips, and dedupe.

Intentional differences from the original plan:

- The client is synchronous rather than async. This keeps the scaffold simple
  while endpoint behavior is still being validated.
- Wallet activity is modeled as the raw source of trade and redemption events
  instead of introducing a separate `PolymarketTrade` model immediately.
- The runner uses subcommands instead of a `--mode` flag.

Remaining:

- Add the Polymarket ingestor test/lint/typecheck steps to CI.
- Decide whether to promote activity rows into dedicated trade/redemption models
  before Phase 3 skill scoring.
- Wire Goldsky into the client if the Data API cannot provide complete history.
- Document local run commands for the Polymarket ingestor in the root README.

## Verification

Local Polymarket ingestor tests pass:

```powershell
cd services/polymarket-ingestor
..\..\.venv\Scripts\python.exe -m pytest -q
```

Result:

```text
20 passed
```
