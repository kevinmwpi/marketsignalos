# MarketSignalOS public website

Next.js displays the wallet leaderboard, held positions, exit signals and
wallet dossiers. The homepage refreshes once per minute while visible and
shows data freshness separately from API connectivity.

## Development

Run `npm ci`, set `API_BASE_URL=http://localhost:8000`, then `npm run dev`.
`NEXT_PUBLIC_API_BASE_URL` is a compatibility fallback; the final default is
`http://localhost:8080`.

For local ingestion buttons, set `SHOW_INGEST_CONTROLS=1` and explicitly enable
`ALLOW_UNAUTHENTICATED_ADMIN=1` on the development API. Production builds hide
these controls. Proxy routes forward only the caller's authorization header;
never configure an operator token as a public frontend variable.

## Deployment and validation

Use [the Railway deployment guide](../../docs/railway-deployment.md), service root
`/apps/web`, and config file `/apps/web/railway.toml`. Set the server-only
`API_BASE_URL` to the cloud API origin. `/api/health` provides a lightweight
liveness check independent of the data feeds.

Run `npm run lint` and `npm run build` before deployment.
