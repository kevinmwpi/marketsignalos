"""
Keep the lean pilot's cohort to wallets whose trading could be followed by a person.

Decided by the owner on 2026-10-02 after the first horizon report: the day-volume
leaderboard had filled the 64-wallet cohort with automation-shaped wallets trading
markets that close within hours. Six of them traded faster than collection could
follow, 70% of fetched bets had no price six hours after entry, and 16% of the
prices one hour after entry already sat at the outcome.

Each run of the ``cohort`` stage, right after scoring:

  1. reads the published score generation and adds every wallet whose
     ``style_archetype`` is ``systematic`` (trader_style.py) to
     ``excluded_wallets.txt``, which only ever grows;
  2. removes every excluded wallet from the watchlist and deletes its rows from
     each per-wallet store, rebuilding the activity dedupe index from the rows
     that remain.

Step 2 is idempotent and runs every time, so a run interrupted halfway is
finished by the next one. Collection never re-adds an excluded wallet
(``run_pipeline(exclude_wallets=...)``), and the freed watchlist slots are
refilled from the leaderboard. Market-keyed stores (markets, entry prices,
closing lines) are kept: other wallets may share those markets.

The label is descriptive, as in trader_style.py: it says a wallet's cadence looks
automated, nothing more.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .score_snapshot import ENRICHMENT, load_current
from .storage import _write_index

log = logging.getLogger("marketsignalos.polymarket.cohort")

EXCLUDED_FILE = "excluded_wallets.txt"
WATCHLIST_FILE = "polymarket_wallet_watchlist.txt"
ACTIVITY_FILE = "polymarket_activity.jsonl"
WALLET_JSONL = (
    ACTIVITY_FILE,
    "polymarket_positions.jsonl",
    "polymarket_position_snapshots.jsonl",
    "polymarket_closed_positions.jsonl",
    "polymarket_wallet_hydration.jsonl",
    "polymarket_wallet_values.jsonl",
)
WALLET_JSON_OBJECTS = (
    "polymarket_wallet_checkpoints.json",
    "polymarket_wallet_economics_cache.json",
)
EXCLUDED_ARCHETYPES = frozenset({"systematic"})


def excluded_wallets(data_dir: Path) -> frozenset[str]:
    """Every wallet ever excluded: the first tab-separated field of each line."""
    path = data_dir / EXCLUDED_FILE
    if not path.exists():
        return frozenset()
    return frozenset(line.split("\t", 1)[0].strip().lower()
                     for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.startswith("#"))


def wallets_to_exclude(enrichment_rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Wallet to archetype, for enrichment rows whose archetype is excluded."""
    found: dict[str, str] = {}
    for row in enrichment_rows:
        wallet = str(row.get("proxy_wallet", "")).lower()
        archetype = str(row.get("style_archetype", ""))
        if wallet and archetype in EXCLUDED_ARCHETYPES:
            found[wallet] = archetype
    return found


def record_exclusions(data_dir: Path, wallets: dict[str, str], *, now: datetime) -> int:
    """Append wallets not already excluded; returns how many were new."""
    known = excluded_wallets(data_dir)
    new = {wallet: reason for wallet, reason in sorted(wallets.items()) if wallet not in known}
    if new:
        with (data_dir / EXCLUDED_FILE).open("a", encoding="utf-8") as out:
            for wallet, reason in new.items():
                out.write(f"{wallet}\t{now.isoformat()}\t{reason}\n")
    return len(new)


def apply_exclusions(data_dir: Path, excluded: frozenset[str]) -> dict[str, int]:
    """Remove excluded wallets from the watchlist and every per-wallet store.

    Rows removed per file. Each file is rewritten atomically and only when it holds
    an excluded wallet, so repeating the call is cheap and finishes an interrupted one.
    """
    removed: dict[str, int] = {}
    if not excluded:
        return removed
    removed[WATCHLIST_FILE] = _filter_watchlist(data_dir / WATCHLIST_FILE, excluded)
    for name in WALLET_JSONL:
        removed[name] = _filter_jsonl(data_dir / name, excluded)
    for name in WALLET_JSON_OBJECTS:
        removed[name] = _filter_json_object(data_dir / name, excluded)
    if removed[ACTIVITY_FILE]:
        _rebuild_activity_index(data_dir / ACTIVITY_FILE)
    return {name: count for name, count in removed.items() if count}


def run(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Lean-pilot stage: exclude systematic wallets, then purge every excluded one."""
    snapshots = data_dir / "score-snapshots"
    newly = 0
    classified = 0
    if (snapshots / "current.json").exists():
        manifest = load_current(snapshots)
        rows = list(_rows(snapshots / manifest["run_id"] / ENRICHMENT))
        classified = len(rows)
        newly = record_exclusions(data_dir, wallets_to_exclude(rows),
                                  now=now or datetime.now(UTC))
    excluded = excluded_wallets(data_dir)
    removed = apply_exclusions(data_dir, excluded)
    if newly or removed:
        log.info("cohort excluded_new=%d excluded_total=%d removed=%s",
                 newly, len(excluded), removed)
    return {"status": "succeeded", "wallets_classified": classified,
            "excluded_new": newly, "excluded_total": len(excluded), "rows_removed": removed}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _filter_watchlist(path: Path, excluded: frozenset[str]) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines
            if not line.strip() or line.startswith("#") or line.strip().lower() not in excluded]
    if len(kept) == len(lines):
        return 0
    _replace_text(path, "\n".join(kept) + "\n")
    return len(lines) - len(kept)


def _filter_jsonl(path: Path, excluded: frozenset[str]) -> int:
    if not path.exists():
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    dropped = 0
    try:
        with path.open(encoding="utf-8") as source, tmp.open("w", encoding="utf-8") as out:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    out.write(line)  # leave unreadable lines exactly as they were
                    continue
                if (isinstance(row, dict)
                        and str(row.get("proxy_wallet", "")).lower() in excluded):
                    dropped += 1
                    continue
                out.write(line)
        if dropped:
            tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return dropped


def _filter_json_object(path: Path, excluded: frozenset[str]) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0
    kept = {key: value for key, value in data.items() if str(key).lower() not in excluded}
    if len(kept) == len(data):
        return 0
    _replace_text(path, json.dumps(kept, separators=(",", ":"), sort_keys=True))
    return len(data) - len(kept)


def _rebuild_activity_index(activity_path: Path) -> None:
    """The dedupe index holds a key per stored row (JsonlActivityStore._dedupe_key).
    An empty or stale index would let duplicates back in, so rebuild it exactly."""
    keys = {f"{row.get('transaction_hash', '')}:{row.get('condition_id', '')}:"
            f"{row.get('outcome_index', 0)}:{row.get('type', '')}"
            for row in _rows(activity_path)}
    _write_index(activity_path.with_suffix(activity_path.suffix + ".index.json"), keys)


def _replace_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row
