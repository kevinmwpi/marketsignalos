# Public research snapshot

The dashboard displays a dated wallet sample even when the scored API has no qualified
signals. This is descriptive source data, not a skill ranking or trade recommendation.
No database, continuous server, Codex automation, or paid resource is added. A small
GitHub Actions job now collects and publishes complete samples every six hours.

## Automatic refresh

`Refresh public research snapshot` runs at 00:17, 06:17, 12:17 and 18:17 UTC. The default
capture makes at most 21 Polymarket requests, or 84 per day. It uses a standard hosted
Linux runner in this public repository, with a ten-minute job timeout, a three-minute
collection budget and a concurrency group that prevents overlapping runs. There are no
OpenAI/model calls and no dependency on the desktop computer or its Codex allowance.

The job runs trusted code from `main`, installs frozen Python dependencies, collects a
candidate, then publishes only a complete capture with all requested wallets and requests
accounted for. Any collection/publication failure leaves the previous public capture in
place. Partial captures can still be reviewed locally but never replace the scheduled
complete capture. The generated JSON records the source commit and Actions run URL.

Publication uses the job's ephemeral repository token, scoped to contents write at the
job level. No personal token or Replit secret is needed. The publisher writes only
`research-snapshot.json` on the dedicated `codex/research-snapshots` branch, initially an
orphan branch. Updates retain previous commits, reject non-fast-forward races, and read
back the resulting ref and verify the uploaded blob digest. Do not manually edit this
generated branch. Application code on `main` is never changed by the scheduled job.

The website reads the public data branch directly through GitHub's raw-content endpoint.
It validates both that capture and its bundled fallback, chooses the newest valid one,
and labels fallback use. A failed refresh cannot replace a newer capture already shown.
Reads have a five-second timeout and 2 MB size cap. The page checks every 15 minutes while
visible; “Refresh research data” checks immediately. It never starts an ingestion job.
Source caching can delay visibility by several minutes. Data changes require no Replit
republish after this frontend integration is deployed.

Captures over eight hours old are marked overdue (six-hour cadence plus two hours of
slack). GitHub schedules are best effort and can be delayed or dropped. Public scheduled
workflows can be disabled after 60 days without repository activity. Use the Actions run
history and capture dates as evidence of execution; the configured schedule alone does
not prove freshness. Git history is convenient pilot retention, not a complete raw event
archive or a long-term warehouse. Review its growth before increasing frequency/coverage.

To run an extra capture, open Actions → Refresh public research snapshot → Run workflow
on `main`, or use `gh workflow run research-snapshot.yml --ref main`. To pause, disable
that workflow in Actions (or `gh workflow disable research-snapshot.yml`). Do not create a
duplicate Codex heartbeat. GitHub's existing workflow notification preferences apply.

References: [standard public-runner billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
and [scheduled-run limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
This does not change Replit's plan, hosting expiration, or any existing Railway service.

## Collect and publish

From the repository root, locally or in the Replit development shell:

```sh
uv sync --frozen --no-dev
uv run --no-sync python scripts/collect_research_snapshot.py --run --wallet-limit 10 --max-seconds 180
uv run --frozen --with pytest python -m pytest scripts/tests/test_research_snapshot.py artifacts/api-server/tests/test_adapter.py -q
pnpm --filter @workspace/marketsignalos-dashboard run typecheck
```

For a manual bundled fallback, review `artifacts/marketsignalos-dashboard/public/data/research-snapshot.json`, commit it
with the collector/UI version, merge the reviewed branch to `main`, pull `main` in Replit,
then republish. Vite copies the file into `dist/public/data/research-snapshot.json`.
Verify the public download's SHA-256 matches the committed file and expand a wallet on
the public page. GitHub updates alone do not prove that Replit has republished.

The September 19 initial bundled capture was collected once on the development computer.
After publication, serving it takes place on Replit and does not require that computer.
Reloading or “Refresh signals” does not collect another snapshot. Restarts restore the
bundled fallback; the scheduled data branch supplies subsequent captures independently.

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
eight hours old, and offers source links and a receipt download. All wallets remain
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

1. Measure scheduled capture reliability and coverage within the $15/month target.
   This bounded public sample now refreshes in the cloud; comprehensive event storage,
   fixed-cohort tracking and authoritative database serving remain future work. Choose
   those resources before paid provisioning. Current total provider spending is unverified.
2. Complete history/settlement coverage and freeze inputs before connecting wallets to
   the existing skill evaluator. Preserve eligibility and uncertainty rules.
3. Pre-register prospective paper-following, freeze prices available at signal time,
   account for delay/spread/fees, and measure performance before presenting an edge.

The snapshot makes the page inspectable. It does not complete comprehensive ingestion,
statistically qualified discovery, or prospective validation.
