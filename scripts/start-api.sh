#!/usr/bin/env bash

set -euo pipefail

PORT="${PORT:-8080}"
export PYTHONPATH="apps/api/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting MarketSignalOS API on 0.0.0.0:${PORT}"

exec uvicorn marketsignalos_api.main:app --host 0.0.0.0 --port "${PORT}"
