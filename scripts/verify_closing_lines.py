"""Check a live closing-line backfill against the answers the price-history probe established.

Run after backfilling the probe's resolved markets twice into the same store. Fails
unless every market closed in 2023 or later came back ``ok`` with its last point at
most two hours before close, every 2021 market came back ``no_history``, and the
second run refetched no market that was already final.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

FINAL = ("ok", "no_history")
MAX_GAP_HOURS = 2.0


def verify(receipts: list[dict[str, Any]], group_of: dict[str, str]) -> tuple[list[str], list[str]]:
    """(problems, report lines). No problems means the backfill matched the probe."""
    first: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        first.setdefault(receipt["condition_id"], receipt)
    per_market = collections.Counter(r["condition_id"] for r in receipts)
    problems = [
        f"refetched a final market: {cid}"
        for cid, r in first.items() if r["status"] in FINAL and per_market[cid] != 1
    ]
    for cid, r in first.items():
        group = group_of.get(cid, "unknown")
        if group >= "resolved-2023" and r["status"] != "ok":
            problems.append(f"{group} {cid}: expected ok, got {r['status']} {r.get('error', '')}")
        if group == "resolved-2021" and r["status"] != "no_history":
            problems.append(f"{group} {cid}: expected no_history, got {r['status']}")
    gaps = [r["hours_last_point_before_close"] for r in first.values() if r["status"] == "ok"]
    if gaps and max(gaps) > MAX_GAP_HOURS:
        problems.append(f"a closing line landed {max(gaps)} h before close")
    table = collections.Counter((group_of.get(c, "unknown"), r["status"]) for c, r in first.items())
    report = [f"{group:<14} {status:<11} {count}" for (group, status), count in sorted(table.items())]
    report.append(f"ok markets: {len(gaps)}; max hours last point before close: "
                  f"{max(gaps) if gaps else None}")
    return problems, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args(argv)
    probe = json.loads(args.probe.read_text(encoding="utf-8"))
    group_of = {str(m["condition_id"]).lower(): m["group"] for m in probe["markets"]}
    receipts = [json.loads(line) for line in args.receipts.read_text().splitlines() if line]
    problems, report = verify(receipts, group_of)
    sys.stdout.write("\n".join(report) + "\n")
    if problems:
        sys.stdout.write("FAILED\n" + "\n".join(problems) + "\n")
        return 1
    sys.stdout.write("VERIFIED: 2023+ markets ok, 2021 markets no_history, no final market refetched\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
