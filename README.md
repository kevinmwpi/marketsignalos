# MarketSignalOS

A Polymarket wallet research dashboard with bounded public data capture and an
experimental ingestion/scoring backend. Observed trading activity is not proof of
forecasting skill or insider activity.

The public dashboard runs on Replit. GitHub Actions collects a small wallet sample
every six hours without Codex/model usage; the dashboard reads each complete
published capture directly. The deeper pilot is opt-in and is not a deployed
continuous collection service.

- [Current runtime and local setup](replit.md)
- [Scheduled research collection](docs/research-snapshot.md)
- [Lean pilot integration, checks and remaining work](docs/lean-pilot-integration.md)
- [Research blueprint and dated evidence](docs/handoff-blueprint.md)

For Python development, run `uv sync --frozen`, then `uv run --no-sync pytest -q`
from this root. The active Python packages remain under `.migration-backup` because
Replit's adapter imports them there; do not remove that directory. The active
frontend is `artifacts/marketsignalos-dashboard`, not the archived Next.js app.

No paid service is provisioned by these files. The $15/month budget is a design
target, not a configured billing cap or measured operating cost.
