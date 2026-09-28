"""Observe Gamma now for a frozen audit's missing candidate conditions, in isolation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import metadata_backfill, polymarket_client
from .lean_pilot import _atomic_json
from .metadata_backfill import BackfillConfig, run_backfill
from .polymarket_client import PolymarketClient


def probe_audit(
    audit_path: Path, output: Path, client: PolymarketClient,
    config: BackfillConfig | None = None,
) -> dict[str, Any]:
    source = audit_path.read_bytes()
    audit = json.loads(source)
    if audit.get("schema_version") != 1 or audit.get("status") != "passed":
        raise ValueError("Expected a passed metadata audit")
    candidates = [row for row in audit["wallets"] if row["metadata_only_candidate"] is True]
    conditions = sorted({cid for row in candidates for cid in row["missing_condition_ids"]})
    if not conditions or any(re.fullmatch(r"0x[0-9a-fA-F]{64}", cid) is None for cid in conditions):
        raise ValueError("Audit must contain valid missing candidate condition IDs")
    output.mkdir(parents=True, exist_ok=False)
    observations = output / "observations.jsonl"

    def persist(rows: list[dict[str, Any]]) -> int:
        observed_at = datetime.now(UTC).isoformat()
        with observations.open("x", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({"recorded_at": observed_at, "source": "gamma/markets",
                                         "market": row}, allow_nan=False) + "\n")
        return len(rows)

    result = run_backfill(client, conditions, directory=output / "ledger",
                          persist=persist, config=config)
    with closing(sqlite3.connect(output / "ledger" / "attempts.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]
        states = [dict(row) for row in db.execute(
            "SELECT * FROM conditions ORDER BY condition_id, closed DESC",
        )]
    stored = {row["condition_id"] for row in states if row["outcome"] == "stored"}
    absent = {cid for cid in conditions if sum(
        row["condition_id"] == cid and row["outcome"] == "not_returned" for row in states
    ) == 2}
    report = {
        "schema_version": 1,
        "scope": "Current Gamma observations for frozen candidate missing IDs; no score replay",
        "source_audit": {"name": audit_path.name, "sha256": hashlib.sha256(source).hexdigest()},
        "source_code_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), Path(metadata_backfill.__file__),
                         Path(polymarket_client.__file__))
        },
        "candidate_wallets": [row["wallet"] for row in candidates],
        "condition_ids": conditions,
        "summary": {"conditions": len(conditions), "returned_now": len(stored),
                    "not_returned_either_filter": len(absent),
                    "inconclusive": len(set(conditions) - stored - absent)},
        "run": result, "attempts": attempts, "condition_observations": states,
        "observations_sha256": (hashlib.sha256(observations.read_bytes()).hexdigest()
                                if observations.exists() else None),
        "limitations": [
            "This does not establish why the historical store lacked a row.",
            "An empty filtered list is not an individual-market HTTP 404 or proof of nonexistence.",
            "No historical market, hydration, activity, score, or qualification file was changed.",
            "Raw responses are kept only in the isolated local output; hashes cover canonical JSON.",
        ],
    }
    if audit_path.read_bytes() != source:
        raise ValueError("Source audit changed during probe")
    _atomic_json(output / "report.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New isolated directory")
    args = parser.parse_args(argv)
    with closing(PolymarketClient()) as client:
        report = probe_audit(args.audit, args.output, client)
    print(json.dumps({"summary": report["summary"], "run": report["run"]}, indent=2))
    return 0 if report["summary"]["inconclusive"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
