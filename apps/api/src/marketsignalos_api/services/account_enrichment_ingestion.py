from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from marketsignalos_api.services.models import AccountEnrichment


def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


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

    latest: dict[str, AccountEnrichment] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        account_id = str(payload.get("account_id", ""))
        if account_id not in account_ids:
            continue

        record = AccountEnrichment(
            account_id=account_id,
            as_of=_parse_utc(str(payload.get("as_of", "1970-01-01T00:00:00Z"))),
            profile_confidence=float(payload.get("profile_confidence", 0.0)),
            cross_market_coordination=float(payload.get("cross_market_coordination", 0.0)),
            pre_resolution_accuracy=float(payload.get("pre_resolution_accuracy", 0.0)),
            fill_intensity=float(payload.get("fill_intensity", 0.0)),
        )

        current = latest.get(account_id)
        if current is None or record.as_of > current.as_of:
            latest[account_id] = record

    return latest
