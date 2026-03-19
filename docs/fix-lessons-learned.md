# Fix Lessons Learned

This log captures what we learn as we close the incomplete MVP gaps one at a time on branch `codex/address-incomplete-tasks`.

## Target Gaps

- Wire trade ingestion into the runner so the API's trade-backed endpoints have real source data.
- Define and implement the account enrichment producer used by the leaderboard API.
- Harden storage and data access beyond repo-local JSONL/JSON files.
- Improve observability and operational health beyond a basic `ok` health response.
- Expand deployment/runtime support for the web app and background ingestion.
- Grow the frontend from a single leaderboard page into a fuller product surface.

## Lessons

### 2026-03-19 - Trade ingestion was implemented but not orchestrated

- Issue: the trade pipeline, store, and worker already existed, but the runner only executed fills and resolutions.
- Fix: wired `KalshiTradeIngestionPipeline`, `JsonlTradeStore`, and `KalshiIngestionWorker` into the runner so each configured ticker now writes `kalshi_trades.jsonl` before downstream analytics consume it.
- Verification: added a runner-level test that exercises trade, fill, and resolution writes together and confirms checkpoint state for all three flows.
- Lesson learned: the biggest MVP gaps here are not always missing algorithms; sometimes the core parts exist but are never connected into the actual runtime path.
- Follow-up: the trade path now exists end-to-end, but the storage layer is still file-backed and append-only, so durability and operational robustness remain separate tasks.
