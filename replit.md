# MarketSignalOS

Public research dashboard for historical Polymarket wallet evidence and open positions.

## Public research data

The dashboard also serves a bounded, dated snapshot from
`artifacts/marketsignalos-dashboard/public/data/research-snapshot.json`. This public
asset is copied into the Vite build as a fallback. The frontend loads the latest valid
capture from GitHub's `codex/research-snapshots` data branch, independently of the signal API.
It shows volume-selected wallets with skill explicitly not evaluated. It never feeds
or relaxes the qualified signal filters. GitHub Actions collects every six hours with
no Codex/model calls. The page checks for updates every 15 minutes while visible and
offers “Refresh research data”; neither starts ingestion. Failed scheduled captures
preserve the previous complete capture. See `docs/research-snapshot.md` for operations,
manual fallback publication and schedule limitations. This UI update requires one Replit
republish; later scheduled data captures do not require redeployments.

## Runtime and routing

- Frontend: React/Vite in `artifacts/marketsignalos-dashboard`, published as static files.
- Backend: Python/FastAPI in `.migration-backup/apps/api/src`, exposed by `artifacts/api-server/api_adapter.py`.
- Replit forwards `/api` without stripping that prefix. The adapter mounts the imported API there; the dashboard uses relative `/api` URLs.
- Both preview and production start `artifacts/api-server/start.py`. The Express/TypeScript files under `artifacts/api-server/src` are unused scaffolding, not the product API.
- Production installs Python dependencies with `uv sync --frozen --no-dev` and starts `uv run --no-sync --offline python artifacts/api-server/start.py`. Build and startup both honor `UV_PROJECT_ENVIRONMENT`; Replit's Python module sets it to `$REPL_HOME/.pythonlibs`. Keep that environment, the root `uv.lock`, and `.migration-backup` Python sources in the published image. Do not hard-code `.venv/bin/python` or exclude `.pythonlibs` from the published image.

The previous production command started the Express placeholder, which only served `/api/healthz`. That explains why the homepage loaded but feed and ingestion requests returned HTML 404 errors. A passing health check alone did not verify the dashboard routes.

The next publication failed with `fork/exec .venv/bin/python: no such file or directory`: installation had succeeded into Replit's configured environment, but startup assumed uv's local default. The surrounding `healthcheck /api returned status 500` messages were a consequence of the server never starting. Use the actual process error in the failed deployment's runtime logs; earlier Node `Server listening` entries belong to the older publication. The current startup command uses the already-built environment without syncing packages or accessing the network. See [Replit's Python module](https://github.com/replit/nixmodules/blob/main/pkgs/modules/python-base/default.nix) and [uv's environment configuration](https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path).

## Applying the production fix

1. Pull/sync the latest reviewed fixes from `main` into the existing Replit project.
2. Keep the API service at port 8080 on `/api`, and the dashboard static service on `/`. Do not point the API service back to the Node placeholder or rewrite `/api/*` to the SPA.
3. Republish the existing app. A GitHub push by itself does not update the running publication.
4. Check these URLs on the published domain: `/api/healthz`, `/api/signals/skilled-bets?limit=1`, `/api/signals/skilled-bets/summary`, `/api/signals/exits?limit=1`, and `/api/signals/polymarket-leaderboard?limit=1`. Each should return HTTP 200 JSON, including when its dataset is empty.
5. Open the dashboard and use **Refresh signals**. It should show `API connected` without a request error. An unknown wallet can legitimately return a JSON 404 explaining that it has no enrichment data.

No secret is required for public reads. Leave `VITE_API_BASE_URL` unset for same-origin `/api` routing. Never place an operator token in a `VITE_*` variable or browser storage.

## Operator access and data

The published service sets `API_READ_ONLY=1`. All write methods and private status/metrics endpoints return 503 even if operator credentials are present. Public reads need no token. Startup refuses background collection settings in this mode. Ingestion and watchlist controls remain hidden in production. No public operator login is implemented.

A separate operator deployment uses the backend's strict production validation: a server-side `ADMIN_API_TOKEN` of at least 32 characters, an absolute data directory, a persistent Railway volume when applicable, and one worker. Operator calls use `Authorization: Bearer ...`; never put that credential in the frontend.

Scheduled ingestion and fast-lane collection are disabled in the published service configuration. This repair restores public reads without starting a collection workload. It provisions no resources and changes no spending settings.

The imported API reads JSONL from `POLYMARKET_DATA_DIR`; a fresh checkout does not include research datasets. Empty feeds do not prove that no skilled wallets exist. `API connected` indicates request success, not fresh data or profitable signals. An optional `DATABASE_URL` enables a mirror; it does not make PostgreSQL the API's read store.

The integration branch reconciles `codex/lean-pilot-15-budget` into the Python package locations used by this adapter. Public freshness status, budget controls, run receipts, and research diagnostics are available there; collection stays disabled on Replit. See `docs/lean-pilot-integration.md` for merge evidence and the current paths. Establish durable storage with a single writer, retention and recovery before activating the pilot. The $15/month target and prepare-before-paid-provisioning instruction still apply.

## Local verification

From the repository root:

```sh
uv sync --frozen
uv run --no-sync pytest -q
pnpm install --frozen-lockfile --ignore-scripts
pnpm --filter @workspace/marketsignalos-dashboard run typecheck
PORT=23002 BASE_PATH=/ NODE_ENV=production pnpm --filter @workspace/marketsignalos-dashboard run build
PORT=8080 uv run --no-sync --offline python artifacts/api-server/start.py
```

The environment-assignment examples above use a POSIX shell; in PowerShell set `$env:PORT`, `$env:BASE_PATH`, and `$env:NODE_ENV` first. The workspace keeps native build packages for Linux x64 (Replit) and Windows x64 (local development). The default frontend production build contains no operator token.

Adapter tests use an isolated empty data directory and disabled collectors. They check real product routes, private-route authorization, mounted startup/shutdown, and a subprocess running the declared launcher. The publication regression test also copies the required sources into a clean temporary project, executes the exact artifact build command into `.pythonlibs`, then executes the exact artifact run command with an empty package cache and network access disabled in uv. It verifies that no `.venv` directory exists, health and feed requests succeed, and anonymous ingestion remains disabled. Package installation for that test can contact the Python package registry; it never contacts Polymarket or starts a production ingestion run.
