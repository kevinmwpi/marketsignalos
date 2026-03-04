from __future__ import annotations

import json
import os
from pathlib import Path

from marketsignalos_api.services.leaderboard_models import AccountEnrichment


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _enrichment_path() -> Path:
    configured = os.getenv("INGESTOR_ACCOUNT_ENRICHMENT_STORE_PATH")
    if configured:
        return Path(configured)
    return _repo_root() / "services" / "ingestor" / "data" / "kalshi_account_enrichment.jsonl"


def collect_account_enrichment(account_ids: set[str]) -> dict[str, AccountEnrichment]:
    path = _enrichment_path()
    if not path.exists() or not account_ids:
        return {}

    enrichment: dict[str, AccountEnrichment] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        row = json.loads(cleaned)
        account_id = str(row.get("account_id", ""))
        if account_id not in account_ids:
            continue
        enrichment[account_id] = AccountEnrichment(
            account_id=account_id,
            cross_market_coordination=float(row.get("cross_market_coordination", 0.0)),
            pre_resolution_accuracy=float(row.get("pre_resolution_accuracy", 0.0)),
            fill_intensity=float(row.get("fill_intensity", 0.0)),
        )
    return enrichment

