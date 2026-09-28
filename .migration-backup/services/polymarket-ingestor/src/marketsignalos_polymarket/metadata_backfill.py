"""Bounded Gamma lookups with crash-conservative, local SQLite attempt receipts.

Receipts describe observations now, never the cause of a historical missing row.
Only this worker shares the ledger lock; callers still need one writer per data dir.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .lean_pilot import _atomic_json, worker_lock
from .polymarket_client import PolymarketClient

PAGE_LIMIT = 100
RETRY_SECONDS = 3600
EMPTY_RETRY_SECONDS = 86400
RECEIPT_LIMIT = 10000


@dataclass(frozen=True)
class BackfillConfig:
    max_conditions: int = 100
    max_requests: int = 8
    batch_size: int = 25

    def __post_init__(self) -> None:
        for key, ceiling in (("max_conditions", 100), ("max_requests", 8), ("batch_size", 25)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{key} must be an integer between 1 and {ceiling}")


def _initialize(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        raise ValueError("Unsupported metadata backfill ledger version")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if (version == 0 and tables) or (
        version == 1
        and tables
        != {
            "attempts",
            "conditions",
            "control",
        }
    ):
        raise ValueError("Invalid metadata backfill ledger; refusing to reset attempt state")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
            closed INTEGER NOT NULL, condition_ids TEXT NOT NULL,
            outcome TEXT NOT NULL, http_status INTEGER, response_sha256 TEXT,
            returned_ids TEXT, next_retry_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conditions (
            condition_id TEXT NOT NULL, closed INTEGER NOT NULL,
            attempted_at REAL NOT NULL, next_retry_at REAL NOT NULL,
            outcome TEXT NOT NULL, PRIMARY KEY(condition_id, closed)
        );
        CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value REAL NOT NULL);
        PRAGMA user_version=1;
    """)


def _retry_after(response: httpx.Response, now: float) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        delay = int(value)
        # Avoid absurd timestamps/float overflow from an untrusted header.
        return now + max(RETRY_SECONDS, min(delay, 365 * 86400))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(now + RETRY_SECONDS, min(parsed.timestamp(), now + 365 * 86400))
        except (TypeError, ValueError, OverflowError):
            return now + RETRY_SECONDS


def _set_conditions(
    db: sqlite3.Connection,
    ids: list[str],
    closed: bool,
    now: float,
    retry_at: float,
    outcome: str,
) -> None:
    db.executemany(
        "INSERT OR REPLACE INTO conditions VALUES (?, ?, ?, ?, ?)",
        [(condition, int(closed), now, retry_at, outcome) for condition in ids],
    )


def _recover(db: sqlite3.Connection, now: float) -> None:
    """A prior process may have sent or stored data. Never invent success."""
    with db:
        for attempt_id, ids_json, closed, retry_at in db.execute(
            "SELECT id, condition_ids, closed, next_retry_at FROM attempts "
            "WHERE outcome IN ('started', 'received')"
        ).fetchall():
            retry_at = max(retry_at, now + RETRY_SECONDS)
            _set_conditions(db, json.loads(ids_json), bool(closed), now, retry_at, "interrupted")
            db.execute(
                "UPDATE attempts SET outcome='interrupted', finished_at=?, next_retry_at=? "
                "WHERE id=?",
                (now, retry_at, attempt_id),
            )
        # Bounded receipt history; condition state expires only after its cooldown.
        db.execute(
            "DELETE FROM attempts WHERE id NOT IN "
            "(SELECT id FROM attempts ORDER BY id DESC LIMIT ?)",
            (RECEIPT_LIMIT,),
        )
        db.execute(
            "DELETE FROM conditions WHERE attempted_at < ? AND next_retry_at <= ?",
            (now - 90 * 86400, now),
        )


def run_backfill(
    client: PolymarketClient,
    condition_ids: Iterable[str],
    *,
    directory: Path,
    persist: Callable[[list[dict[str, Any]]], int],
    config: BackfillConfig | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Fetch at most 100 conditions / eight HTTP attempts, including error attempts.

    Each request is reserved before transport. No retries or sleeps occur inside a
    cycle. Data is persisted once per cycle; returned != stored until that succeeds.
    An empty filtered response means only 'not returned', never individual HTTP 404.
    """
    config = config or BackfillConfig()
    directory.mkdir(parents=True, exist_ok=True)
    with (
        worker_lock(directory / "backfill.lock"),
        closing(sqlite3.connect(directory / "attempts.sqlite3", timeout=0)) as db,
    ):
        _initialize(db)
        now = clock()
        _recover(db, now)
        candidates = sorted(set(condition_ids))
        state = {
            (row[0], bool(row[1])): (row[2], row[3])
            for row in db.execute(
                "SELECT condition_id, closed, attempted_at, next_retry_at FROM conditions"
            )
        }
        cooldown = db.execute("SELECT value FROM control WHERE key='retry_at'").fetchone()
        blocked_until = cooldown[0] if cooldown else 0.0
        eligible: dict[str, list[bool]] = {}
        for condition in candidates:
            if not isinstance(condition, str) or not condition or len(condition) > 128:
                raise ValueError("Invalid condition ID")
            sides = [
                side for side in (True, False) if state.get((condition, side), (0, 0))[1] <= now
            ]
            if sides:
                eligible[condition] = sides
        # Never-attempted / oldest-attempted first, so repeated misses cannot starve
        # the rest of a large backlog. Allocation is across both closed filters.
        selected = heapq.nsmallest(
            config.max_conditions,
            eligible,
            key=lambda condition: (
                min(state.get((condition, side), (0, 0))[0] for side in eligible[condition]),
                condition,
            ),
        )
        report: dict[str, Any] = {
            "schema_version": 1,
            "started_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "limits": asdict(config),
            "candidate_conditions": len(candidates),
            "eligible_lookups": sum(map(len, eligible.values())),
            "cooldown_lookups": 2 * len(candidates) - sum(map(len, eligible.values())),
            "requests": 0,
            "attempted_lookups": 0,
            "deferred_lookups": 0,
            "rows_written": 0,
            "outcomes": {},
            "status": "completed",
            "stored_lookups": 0,
            "not_returned_lookups": 0,
        }
        raw: list[dict[str, Any]] = []
        received: list[tuple[int, list[str], bool, set[str], float]] = []
        stop = blocked_until > now
        batches: list[tuple[float, bool, list[str]]] = []
        for closed in (True, False):
            ids = sorted(
                (condition for condition in selected if closed in eligible[condition]),
                key=lambda condition: (state.get((condition, closed), (0, 0))[0], condition),
            )
            for start in range(0, len(ids), config.batch_size):
                batch = ids[start : start + config.batch_size]
                batches.append((state.get((batch[0], closed), (0, 0))[0], closed, batch))
        # Stable sort preserves closed/open order on a first visit, while an
        # unvisited open filter outranks retried closed filters on later visits.
        batches.sort(key=lambda item: item[0])
        for _, closed, batch in batches:
            if stop or report["requests"] >= config.max_requests:
                break
            started = clock()
            retry_at = started + RETRY_SECONDS
            with db:
                cursor = db.execute(
                    "INSERT INTO attempts (started_at, closed, condition_ids, outcome, "
                    "next_retry_at) VALUES (?, ?, ?, 'started', ?)",
                    (started, int(closed), json.dumps(batch), retry_at),
                )
                attempt_id = cursor.lastrowid
                assert attempt_id is not None
                _set_conditions(db, batch, closed, started, retry_at, "started")
            report["requests"] += 1
            report["attempted_lookups"] += len(batch)
            status: int | None = None
            digest = None
            returned: set[str] = set()
            try:
                rows = client.get_markets_once(batch, closed=closed, limit=PAGE_LIMIT)
                status = 200
                digest = hashlib.sha256(
                    json.dumps(
                        rows,
                        sort_keys=True,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                # Saturation or an ignored condition filter is not a complete lookup.
                if len(rows) >= PAGE_LIMIT:
                    outcome = "response_limit"
                elif any(row.get("conditionId") not in batch or not row.get("id") for row in rows):
                    outcome = "invalid_response"
                else:
                    returned = {row["conditionId"] for row in rows}
                    raw.extend(rows)
                    outcome = "received"
                    received.append((attempt_id, batch, closed, returned, started))
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                outcome = "http_error"
                retry_at = _retry_after(exc.response, clock())
                blocked_until = retry_at
                # Stop on endpoint errors instead of charging the next batch too.
                stop = True
                with db:
                    db.execute("INSERT OR REPLACE INTO control VALUES ('retry_at', ?)", (retry_at,))
            except httpx.TransportError:
                outcome = "transport_error"
                blocked_until = retry_at
                stop = True
                with db:
                    db.execute("INSERT OR REPLACE INTO control VALUES ('retry_at', ?)", (retry_at,))
            except (ValueError, TypeError):
                outcome = "invalid_response"
            with db:
                db.execute(
                    "UPDATE attempts SET outcome=?, finished_at=?, http_status=?, "
                    "response_sha256=?, returned_ids=?, next_retry_at=? WHERE id=?",
                    (
                        outcome,
                        clock(),
                        status,
                        digest,
                        json.dumps(sorted(returned)),
                        retry_at,
                        attempt_id,
                    ),
                )
                _set_conditions(db, batch, closed, started, retry_at, outcome)
            report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
        try:
            report["rows_written"] = persist(raw) if raw else 0
            if report["rows_written"] != len(raw):
                raise ValueError("Metadata persistence did not confirm every returned row")
        except Exception:
            with db:
                for attempt_id, batch, closed, _, started in received:
                    db.execute(
                        "UPDATE attempts SET outcome='storage_error' WHERE id=?", (attempt_id,)
                    )
                    _set_conditions(
                        db, batch, closed, started, clock() + RETRY_SECONDS, "storage_error"
                    )
            report["status"] = "storage_error"
            report["outcomes"]["storage_error"] = report["outcomes"].pop("received", 0)
            report["deferred_lookups"] = report["eligible_lookups"] - report["attempted_lookups"]
            _finish_report(report, directory, clock())
            raise
        with db:
            for attempt_id, batch, closed, returned, started in received:
                db.execute("UPDATE attempts SET outcome='recorded' WHERE id=?", (attempt_id,))
                for condition in batch:
                    outcome = "stored" if condition in returned else "not_returned"
                    report[f"{outcome}_lookups"] += 1
                    retry_at = clock() + (
                        RETRY_SECONDS if condition in returned else EMPTY_RETRY_SECONDS
                    )
                    _set_conditions(db, [condition], closed, started, retry_at, outcome)
            db.execute(
                "DELETE FROM attempts WHERE id NOT IN "
                "(SELECT id FROM attempts ORDER BY id DESC LIMIT ?)",
                (RECEIPT_LIMIT,),
            )
        if received:
            report["outcomes"]["recorded"] = report["outcomes"].pop("received")
        report["deferred_lookups"] = report["eligible_lookups"] - report["attempted_lookups"]
        if (
            report["deferred_lookups"]
            or report["cooldown_lookups"]
            or any(key != "recorded" for key in report["outcomes"])
        ):
            report["status"] = "partial"
        report["upstream_retry_at"] = blocked_until if blocked_until > now else None
        _finish_report(report, directory, clock())
        return report


def _finish_report(report: dict[str, Any], directory: Path, now: float) -> None:
    report["finished_at"] = datetime.fromtimestamp(now, UTC).isoformat()
    _atomic_json(directory / "latest.json", report)
