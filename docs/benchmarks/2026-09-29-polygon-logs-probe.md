# Can Polymarket trades be read straight from Polygon? Live probe of `eth_getLogs`

Run twice on 2026-09-29 on GitHub Actions with `scripts/probe_polygon_logs.py`:
[run 36520936026](https://github.com/kevinmwpi/marketsignalos/actions/runs/36520936026)
(commit `22b321e`, 04:17–04:19 UTC) and
[run 36521364258](https://github.com/kevinmwpi/marketsignalos/actions/runs/36521364258)
(commit `a5d9e22`, 04:23–04:25 UTC, which added the per-wallet range test).
Read-only, public endpoints only, no API keys or paid services. Measurements:
[JSON](2026-09-29-polygon-logs-probe.json).

## Why this was run

The idea under test: read Polymarket fills from the Polygon chain instead of the
Polymarket Data API, to cut cost and stop the API from bottlenecking research.
Two real limits make it worth checking. Wallet discovery is limited to leaderboard
names, and per-wallet activity pagination is capped. The question was what reading
the chain costs in practice, not whether it is possible.

Event layouts were taken from Polymarket's contract source, not from memory:
`OrderFilled` in `ctf-exchange` (V1, `ITrading.sol`) and in `ctf-exchange-v2` at
`ccc0596` (`Events.sol`). V2 adds a `side` field and a single `tokenId`. Topic
hashes were computed by the node (`web3_sha3`) after it reproduced the ERC-20
`Transfer` topic.

## Result

### Decoding is correct

In each run, 15 recent Data API trades were checked against the `OrderFilled`
events decoded from the same transaction. All 30 matched: same wallet, same token,
size within 1% and price within 1 cent. All 30 decoded as V2 fills (28 buys, 2 sells).

Every one of the 30 wallets appeared as `maker` in its own fill. The events show
why: a match emits one event per resting order, plus one for the taker's order, in
which the taker wallet is `maker` and the exchange contract is `taker`. So filtering
on the maker topic finds a wallet's fills whichever side it traded on. This held for
all 30 trades checked; it has not been proved for every order type.

### Public RPC endpoints are fragile

| Endpoint | Run 1 | Run 2 |
|---|---|---|
| publicnode | usable (head + `web3_sha3` verified) | usable |
| polygon-rpc.com | 403, "API key disabled, tenant disabled" | same |
| drpc | head only; `web3_sha3` not verified | same |
| 1rpc | usage limit reached | head only; `web3_sha3` not verified |

Only one of the four free endpoints was fully usable in both runs. Every measurement
below comes from publicnode.

### Following every trade means about 2.9 GB a day

V2 `OrderFilled` events from both V2 exchanges, unfiltered, by block range ending at
the chain head (1.5 s per block):

| Blocks | ≈ Time | Events (run 1 / run 2) | Response size | Seconds |
|---:|---:|---:|---:|---:|
| 100 | 2.5 min | 3,582 / 4,697 | 3.9 / 5.1 MB | 14.2 / 0.4 |
| 500 | 12.5 min | 23,141 / 22,365 | 25 / 24 MB | 3.1 / 2.2 |
| 1,000 | 25 min | 50,863 / 48,652 | 55 / 53 MB | 5.7 / 5.3 |
| 2,000 | 50 min | 100,272 / 102,175 | 109 / 111 MB | 13.1 / 14.5 |
| 5,000 | 2.1 h | 248,188 / 246,997 | 270 / 269 MB | 28.6 / 28.9 |
| 10,000 | 4.2 h | timed out at 30 s | — | — |

Density was 44.7–46.3 events per block. At 57,600 blocks a day that is 2.6–2.7 million
events and **2.8–2.9 GB of raw JSON-RPC responses a day** (about 1,089 bytes per
event). Events overstate trades, because each match emits one event per order on
both sides.

### One wallet's history takes about 970 calls

Filtered to a single wallet as maker, a 5,000-block query returned 3–7 fills in
about 50 ms. publicnode rejects any range wider than 10,000 blocks ("exceed maximum
block range: 10000"). From 2026-04-01 (block 84,940,782) to the head is 9.69 million
blocks, so **one wallet's V2-era history takes about 970 calls**, before any rate
limiting. The Data API returns the same history in a few paginated calls.

### Earlier history is gone from public nodes

- V1 exchange logs for June 2025 (block 72,251,473) returned "History has been pruned
  for this block" on publicnode. How far back it keeps logs was not measured; June
  2025 is already gone.
- The V1 exchanges emitted 0 fills in the most recent 500 blocks in both runs, so all
  current trading is on V2.
- History before 2026 therefore needs an archive node or an indexer, not a free RPC.

### The Goldsky subgraph this repository uses is shut down

The Goldsky orderbook subgraph returned HTTP 429 `ENDPOINT_DEPRECATED` in both runs:
"paused and deprecated following Polymarket's migration to V2 — the data is stale
and incorrect. Stop using it." It points to the Goldsky Edge Data API instead. That
API was not tested, and its pricing was not checked.

In this repository, `GOLDSKY_SUBGRAPH` in `polymarket_client.py` backs two things:

- `get_recent_trader_wallets`, the deep sweep's recent-trader discovery. It is on by
  default and skipped with `--no-recent-traders`.
- `get_wallet_order_fills_from_subgraph`, the activity fallback used by
  `--exhaust-activity` and `try_subgraph_backfill`.

The client treats 429 as retryable, so both paths now spend their full retry
budget on an error that will never clear, then fail. The lean pilot worker and the
scheduled research snapshot do not touch the subgraph: collection calls
`run_pipeline` with subgraph backfill off.

*Update 2026-10-08:* both paths were removed, and no code calls the subgraph. The
fallback was reachable only from the deep pipeline, which hydrates with
`exhaust_activity=True`; there was no `--exhaust-activity` CLI flag. A deep run
now says "subgraph discovery unavailable (deprecated 2026)" in its `warning`.
This probe's subgraph check was removed too.

## What this means

1. **The chain is not cheaper than the Data API; it is more complete.** The Data API
   costs nothing and returns a wallet's history in a few calls. The chain costs
   bandwidth (about 2.9 GB a day to follow every trade) or many calls (about 970 per
   wallet's V2 history). It also depends on free RPC endpoints: three of four were
   unusable or only partly working, and the one that worked has already pruned 2025. Moving
   everything to the chain moves the bottleneck from Polymarket to the RPC provider.
2. **Where the chain clearly wins is discovery.** Reading the chain forward from its
   head finds every wallet that trades, not only leaderboard names. Covering every
   block costs about 12 calls of 5,000 blocks (2.9 GB) a day on a free node; sampling
   one 500-block window an hour costs about 600 MB. No history backfill is needed.
   That also replaces the discovery the dead subgraph used to provide. The block
   timestamp gives an exact event time for the bitemporal store (blueprint §8).
3. **This confirms blueprint Stage 4's non-goal.** Chain logs give the population; the
   Data API gives the enriched history. Use both. For Stage 4, use the exchange
   `OrderFilled` events, which carry side and price. Add ERC-1155 transfers only for
   splits, merges and redemptions, which never pass through the exchange.
4. **Pre-2026 history on-chain needs a provider decision.** That means a paid archive
   RPC or an indexer, with a cost line in blueprint §9. Nothing here justifies paying
   for one before Stage 3 has a result.

## Limits

- Two runs, minutes apart, on one day. Density changes with market activity, and the
  daily figures are extrapolated from 500-block windows.
- Only one RPC provider was measured. Other providers have different range caps,
  timeouts and retention.
- 2026-04-01 is the probe's approximation of the V2 cutover, not a measured date.
- The per-wallet test traced three low-activity wallets. A market maker's fills in a
  10,000-block window could be large enough to hit response limits.
- Matching the Data API proves decoding for ordinary buys and sells. Fees, NegRisk
  conversions, splits, merges and redemptions were not checked.
