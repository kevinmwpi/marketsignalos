# Public research snapshot

The dashboard displays a dated wallet sample even when the scored API has no qualified
signals. This is descriptive source data, not a skill ranking or trade recommendation.
No authentication, database, continuous worker, or paid resource is added.

## Collect and publish

From the repository root, locally or in the Replit development shell:

```sh
uv sync --frozen --no-dev
uv run --no-sync python scripts/collect_research_snapshot.py --run --wallet-limit 10 --max-seconds 180
uv run --frozen --with pytest python -m pytest scripts/tests/test_research_snapshot.py artifacts/api-server/tests/test_adapter.py -q
pnpm --filter @workspace/marketsignalos-dashboard run typecheck
```

Review `artifacts/marketsignalos-dashboard/public/data/research-snapshot.json`, commit it
with the collector/UI version, merge the reviewed branch to `main`, pull `main` in Replit,
then republish. Vite copies the file into `dist/public/data/research-snapshot.json`.
Verify the public download's SHA-256 matches the committed file and expand a wallet on
the public page. GitHub updates alone do not prove that Replit has republished.

The September 19 initial capture was collected once on the development computer.
After publication, serving it takes place on Replit and does not require that computer.
Reloading or “Refresh signals” does not collect another snapshot. Restarts restore the
bundled capture. Recollect, review, commit, sync and republish to refresh the public data.

## Bounds and evidence

- Selection: first page of OVERALL / DAY / VOL on the public leaderboard, default 10
  wallets, maximum 20. This favors active accounts; it is not an unbiased population
  sample or evidence of excess return.
- At most one leaderboard request, one trade request and one positions request per
  unique wallet: default 21, maximum 41 HTTP attempts. No retries or pagination.
- Trades: at most 100 newest TRADE rows in a fixed seven-day window ending at collection
  start. Holdings: at most 50 positive-size, non-redeemable rows ordered by reported value.
  Upstream default archive filtering applies. These samples are incomplete.
- A full page is labelled possibly truncated. A shorter page does not prove complete
  lifetime history. Two identical fills can be legitimate: trade identity is not inferred
  from transaction hashes and fills are not collapsed. Duplicate position assets are
  excluded and counted.
- The 180-second default budget (maximum 300) prevents new requests after expiry and is
  checked while reading responses. An in-flight network read can take its remaining
  timeout (at most eight seconds). Each decoded response is capped at 1 MB; output at 2 MB.
- Receipts record endpoint, parameters, request/capture time, status, response digest,
  returned rows and excluded rows. The digest identifies a source response; the file
  retains normalized observations, not the full original response bytes.
- Missing numeric values remain null. Wrong-wallet rows, malformed identifiers, invalid
  required values, out-of-window trades and duplicate holdings are excluded. Failed
  requests are unavailable, not zero holdings. Partial success is labelled partial.
- No usable observations or a failed/oversized write preserves the previous file.
  Replacement is atomic. The legacy JSONL/scoring archive is never written.

The UI validates the schema, displays capture and per-request times, marks captures over
24 hours old, and offers source links and a receipt download. All wallets remain
`not_evaluated`. Holdings may be settled or no longer tradable; a reported mark is not an
executable price. Prices, balances and market state are not synchronized. Fees, complete
cash flows, resolutions, profitability and forecast skill are not inferred. The original
signal API and its eligibility filters are unchanged.

Official endpoint references (legacy v1, verified September 19, 2026):
[leaderboard](https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings),
[activity](https://docs.polymarket.com/api-reference/core/get-user-activity),
[positions](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user).
Evaluate the documented v2 migration before expanding coverage; do not silently combine
schemas or change the cohort definition.

## Next stages

1. Durable cloud collection and a measured refresh schedule within the $15/month target.
   Choose storage and worker placement before paid provisioning. This pass creates
   neither and does not establish current provider spending.
2. Complete history/settlement coverage and freeze inputs before connecting wallets to
   the existing skill evaluator. Preserve eligibility and uncertainty rules.
3. Pre-register prospective paper-following, freeze prices available at signal time,
   account for delay/spread/fees, and measure performance before presenting an edge.

The snapshot makes the page inspectable. It does not complete continuous ingestion,
statistically qualified discovery, or prospective validation.
