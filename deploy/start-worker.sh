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

config="$(dirname "$0")/lean-pilot.json"

# Operator-triggered repair after an interrupted collection (pilot_recovery.py). Set
# PILOT_RECOVER to the run id a "recovery_required" log line names. It repairs only
# that incident: a later one has another id and is refused, so a variable left set is
# never an automatic repair. A refused or failed recovery exits nonzero here.
if [[ -n "${PILOT_RECOVER:-}" ]]; then
  python -m marketsignalos_polymarket.lean_pilot \
    --data-dir "${root}/pilot" --config "${config}" --recover "${PILOT_RECOVER}"
fi

exec python -m marketsignalos_polymarket.lean_pilot \
  --data-dir "${root}/pilot" --config "${config}" "${@:---run}"
