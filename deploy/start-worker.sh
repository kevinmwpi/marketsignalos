#!/usr/bin/env bash
# Entry point of the lean pilot worker image. Runs due stages once and exits, which
# is what a Railway cron service requires. Pass --plan for a read-only check.
set -euo pipefail

# Cadence and the daily runtime allowance are persisted under the data directory.
# On a container filesystem they would reset every run, so refuse to start
# without a volume rather than silently lose the budget accounting.
root="${POLYMARKET_PILOT_DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-}}"
if [[ -z "${root}" ]]; then
  echo "No data directory: attach a volume, or set POLYMARKET_PILOT_DATA_DIR." >&2
  exit 2
fi
if [[ -n "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is set; the lean pilot is JSONL-only. Unset it for this service." >&2
  exit 2
fi

exec python -m marketsignalos_polymarket.lean_pilot \
  --data-dir "${root}/pilot" --config "$(dirname "$0")/lean-pilot.json" "${@:---run}"
