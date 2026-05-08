from __future__ import annotations

import json
from pathlib import Path

from marketsignalos_api._paths import enrichment_store_path
from marketsignalos_api.services.leaderboard_models import AccountEnrichment


def _read_jsonl_enrichment(path: Path, account_ids: set[str]) -> dict[str, AccountEnrichment]:
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


def _collect_from_db(account_ids: set[str]) -> dict[str, AccountEnrichment]:
    from marketsignalos_api.db import db_cursor

    with db_cursor() as cur:
        cur.execute(  # type: ignore[union-attr]
            """
            SELECT account_id, cross_market_coordination,
                   pre_resolution_accuracy, fill_intensity
            FROM account_enrichment
            WHERE account_id = ANY(%s)
            """,
            (list(account_ids),),
        )
        rows: list[dict[str, object]] = [dict(r) for r in cur.fetchall()]  # type: ignore[union-attr]

    return {
        str(row["account_id"]): AccountEnrichment(
            account_id=str(row["account_id"]),
            cross_market_coordination=float(row["cross_market_coordination"]),
            pre_resolution_accuracy=float(row["pre_resolution_accuracy"]),
            fill_intensity=float(row["fill_intensity"]),
        )
        for row in rows
    }


def collect_account_enrichment(account_ids: set[str]) -> dict[str, AccountEnrichment]:
    from marketsignalos_api.db import get_database_url

    if not account_ids:
        return {}
    if get_database_url():
        return _collect_from_db(account_ids)
    return _read_jsonl_enrichment(enrichment_store_path(), account_ids)
