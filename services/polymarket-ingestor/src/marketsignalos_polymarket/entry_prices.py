"""
Backfill hourly prices for the hours after each tracked buy (post-entry CLV).

Blueprint open decision 6 was settled on 2026-09-30: gate 13's reference price for a
bet is the market's price a fixed time *after the buy*, not the last price before
close, which was within 0.01 of the outcome for 38 of 40 probed markets. The horizon
is not fixed yet; a diagnostic on pilot data picks it, anywhere up to
``HORIZON_SECONDS``. This module therefore stores every hourly point from each buy
to that long after it. The diagnostic chose 1 h on 2026-10-05, and forecast-v5 reads
these prices for gate 13 (post_entry_clv.py).

``HORIZON_SECONDS`` was seven days until 2026-10-03, when the owner limited the
candidate horizons to 1 h and 6 h: 95% of the cohort's bets were on markets that
closed within a week, so longer horizons had no price to read. Chunks fetched for
the longer window stay in the store; the history remains fetchable if a later
cohort trades longer-lived markets.

History is fetched in fixed chunks of ``CHUNK_SECONDS`` aligned to the Unix epoch and
keyed by (condition id, chunk start):

  - a chunk is fetched only after it has ended, so its prices can no longer change,
    and a final chunk (``ok`` or ``empty``) is never fetched again;
  - new buys only add the chunks they reach;
  - the YES token is fetched, matching how CLV reads prices; NO is 1 - YES;
  - each receipt keeps the market's actual close time (Gamma ``closedTime``) when the
    token lookup saw one, so the horizon diagnostic can tell a market that closed
    before a horizon from a gap in its series.

A post-entry price is outcome-free only if the outcome was still unknown at the
horizon. Markets that settle within it stop publishing points, and ``price_after``
returns None for them; an outcome that is known before the market closes is not
detected here. The horizon diagnostic has to measure how often the reference already
sits within 0.01 of 0 or 1, as the closing-line check did.

Rows use the ``price_observations`` contract (docs/handoff-blueprint.md section 8):
each point keeps the time it was true and the time it was fetched, so a rescoring of a
frozen snapshot can exclude anything fetched after it (``observed_before``). Both
files are append-only; a torn final line from a crash is ignored.

Observations are compressed once a pass is done (owner's approval, 2026-10-06; the
store grew about 68 MB a day uncompressed against a 5 GB volume). ``backfill`` still
appends plain rows to ``OBSERVATIONS_FILE``, so a chunk's rows are on disk before its
receipt exactly as before. ``compact_observations`` then moves every complete row into
``ARCHIVE_FILE`` as one more gzip member and removes the plain file. Readers take the
archive first and the plain file after it, so the rows, their order and therefore
every score are unchanged.
"""
from __future__ import annotations

import bisect
import gzip
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable, Iterator
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from .closing_lines import (
    ACTIVITY_FILE,
    CLOB_BASE_URL,
    FIDELITY_MINUTES,
    GAMMA_BATCH,
    HEADERS,
    GetJson,
    PriceObservation,
    _iso,
    _parse_lines,
    _parse_time,
    _read_jsonl,
    _utcnow_iso,
    http_get_json,
    parse_close,
    parse_token_ids,
)
from .market_rules import GAMMA_BASE_URL

log = logging.getLogger("marketsignalos.polymarket.entry_prices")

STORE_DIR = "entry_prices"
OBSERVATIONS_FILE = "price_observations.jsonl"
ARCHIVE_FILE = "price_observations.jsonl.gz"
RECEIPTS_FILE = "chunk_receipts.jsonl"
HORIZON_SECONDS = 6 * 3600  # the longest candidate horizon (horizon_diagnostic)
CHUNK_SECONDS = 7 * 86400
# A chunk counts as ended this long after its last hour, so late points are in.
SETTLE_SECONDS = 3600
FINAL_STATUSES = frozenset({"ok", "empty"})
RETRY_AFTER = timedelta(hours=24)

# (condition id, chunk start in Unix seconds)
ChunkKey = tuple[str, int]


class MarketLookup(NamedTuple):
    yes_token: str
    closed_time: str  # Gamma closedTime as ISO-8601 UTC; empty while open or unknown


@dataclass(frozen=True, slots=True)
class ChunkReceipt:
    condition_id: str
    chunk_start: str
    chunk_end: str
    status: str  # ok | empty | no_token | http_error
    observed_time: str
    token_id: str = ""
    points: int = 0
    fidelity_minutes: int = FIDELITY_MINUTES
    error: str = ""
    market_closed_time: str = ""


@dataclass(slots=True)
class BackfillSummary:
    requested: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    observations_written: int = 0
    stopped_early: bool = False


def chunk_start(ts: int) -> int:
    return ts - ts % CHUNK_SECONDS


def needed_chunks(activity_path: Path, *, horizon_seconds: int = HORIZON_SECONDS) -> set[ChunkKey]:
    """Every chunk reached by the window from a BUY to ``horizon_seconds`` after it.

    Streams the activity store the way the ingestor does, skipping unparseable lines.
    """
    needed: set[ChunkKey] = set()
    if not activity_path.exists():
        return needed
    with activity_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("type") != "TRADE" or row.get("side") != "BUY":
                continue
            cid = str(row.get("condition_id", "")).strip().lower()
            ts = row.get("timestamp")
            if not cid or isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
                continue
            for start in range(chunk_start(ts), ts + horizon_seconds + 1, CHUNK_SECONDS):
                needed.add((cid, start))
    return needed


def latest_receipts(
    store: Path, *, observed_before: datetime | None = None,
) -> dict[ChunkKey, tuple[str, datetime]]:
    """Latest (status, observed time) per chunk, optionally as of a cutoff."""
    latest: dict[ChunkKey, tuple[str, datetime]] = {}
    for row in _read_jsonl(store / RECEIPTS_FILE):
        observed = _parse_time(row.get("observed_time"))
        start = _parse_time(row.get("chunk_start"))
        if observed is None or start is None:
            continue
        if observed_before is not None and observed > observed_before:
            continue
        cid = str(row.get("condition_id", "")).lower()
        if cid:
            latest[(cid, int(start.timestamp()))] = (str(row.get("status", "")), observed)
    return latest


def close_times(store: Path) -> dict[str, int]:
    """Actual close time (Unix seconds) per market, from the latest receipt that
    recorded one. Receipts written before 2026-10-03 never do."""
    found: dict[str, int] = {}
    for row in _read_jsonl(store / RECEIPTS_FILE):
        closed = _parse_time(row.get("market_closed_time"))
        cid = str(row.get("condition_id", "")).lower()
        if cid and closed is not None:
            found[cid] = int(closed.timestamp())
    return found


def select_pending(
    needed: Iterable[ChunkKey], receipts: dict[ChunkKey, tuple[str, datetime]], *,
    now: datetime, limit: int, retry_after: timedelta = RETRY_AFTER,
    priority: AbstractSet[ChunkKey] = frozenset(),
) -> list[ChunkKey]:
    """Chunks to fetch next: ended and never tried first (newest first), then retries,
    with every ``priority`` chunk ahead of every other one in the same order.

    Chunks that have not ended yet wait, so every fetch is final. Final chunks are
    skipped for good; a failed chunk waits ``retry_after`` before another attempt.
    Newest first because recent markets have order-book history and matter most for
    recent-edge scoring; 2021-2022 markets have none and come last. ``priority`` holds
    the chunks a pending decision is waiting on (the horizon diagnostic's bets), which
    would otherwise queue behind every newer buy.
    """
    cutoff = int(now.timestamp()) - SETTLE_SECONDS
    fresh: list[ChunkKey] = []
    retry: list[tuple[datetime, ChunkKey]] = []
    for key in needed:
        if key[1] + CHUNK_SECONDS > cutoff:
            continue
        receipt = receipts.get(key)
        if receipt is None:
            fresh.append(key)
        elif receipt[0] not in FINAL_STATUSES and now - receipt[1] >= retry_after:
            retry.append((receipt[1], key))
    fresh.sort(key=lambda k: (-k[1], k[0]))
    retry.sort()
    ordered = fresh + [key for _, key in retry]
    ordered = ([key for key in ordered if key in priority]
               + [key for key in ordered if key not in priority])
    return ordered[:max(0, limit)]


def lookup_markets(condition_ids: list[str], get: GetJson) -> dict[str, MarketLookup]:
    """YES token and actual close time per condition id. Gamma filters on ``closed``,
    so ask under both."""
    found: dict[str, MarketLookup] = {}
    for closed in ("false", "true"):
        remaining = [cid for cid in condition_ids if cid not in found]
        for start in range(0, len(remaining), GAMMA_BATCH):
            batch = remaining[start:start + GAMMA_BATCH]
            rows = get(f"{GAMMA_BASE_URL}/markets",
                       {"condition_ids": batch, "closed": closed, "limit": 100})
            if not isinstance(rows, list):
                raise ValueError(f"Gamma /markets: expected a list, saw {type(rows).__name__}")
            wanted = set(batch)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("conditionId", "")).lower()
                ids = parse_token_ids(row.get("clobTokenIds"))
                if cid in wanted and ids:
                    close_ts, close_field = parse_close(row)
                    found[cid] = MarketLookup(
                        ids[0], _iso(close_ts) if close_ts is not None
                        and close_field == "closedTime" else "")
    return found


def fetch_chunk(token_id: str, start: int, get: GetJson) -> list[tuple[int, float]]:
    """1-hour points in [start, start + CHUNK_SECONDS), sorted by time."""
    payload = get(f"{CLOB_BASE_URL}/prices-history", {
        "market": token_id, "startTs": start, "endTs": start + CHUNK_SECONDS,
        "fidelity": FIDELITY_MINUTES,
    })
    rows = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("CLOB /prices-history: expected {'history': [...]}")
    points = [(int(row["t"]), float(row["p"])) for row in rows
              if isinstance(row, dict) and isinstance(row.get("t"), (int, float))
              and isinstance(row.get("p"), (int, float))
              and start <= row["t"] < start + CHUNK_SECONDS]
    return sorted(points)


def backfill(
    chunks: list[ChunkKey], store: Path, *, get: GetJson,
    deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
) -> BackfillSummary:
    """Fetch the given chunks, writing each chunk's rows before its receipt.

    No new chunk starts once ``deadline`` (on ``clock``) has passed; chunks left over
    have no receipt and come back in the next selection.
    """
    summary = BackfillSummary(requested=len(chunks))
    if not chunks:
        return summary
    store.mkdir(parents=True, exist_ok=True)
    markets = lookup_markets(sorted({cid for cid, _ in chunks}), get)
    with (store / OBSERVATIONS_FILE).open("a", encoding="utf-8") as obs_out, \
            (store / RECEIPTS_FILE).open("a", encoding="utf-8") as receipt_out:
        for cid, start in chunks:
            if deadline is not None and clock() >= deadline:
                summary.stopped_early = True
                break
            observed = _utcnow_iso()
            market = markets.get(cid, MarketLookup("", ""))
            receipt = ChunkReceipt(cid, _iso(start), _iso(start + CHUNK_SECONDS), "",
                                   observed, token_id=market.yes_token,
                                   market_closed_time=market.closed_time)
            observations: list[PriceObservation] = []
            if not receipt.token_id:
                receipt = _with(receipt, status="no_token")
            else:
                try:
                    points = fetch_chunk(receipt.token_id, start, get)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    receipt = _with(receipt, status="http_error", error=str(exc)[:200])
                else:
                    observations = [
                        PriceObservation(cid, receipt.token_id, 0, _iso(t), observed, p,
                                         FIDELITY_MINUTES) for t, p in points
                    ]
                    receipt = _with(receipt, status="ok" if points else "empty",
                                    points=len(points))
            for observation in observations:
                obs_out.write(json.dumps(asdict(observation), separators=(",", ":")) + "\n")
            obs_out.flush()
            receipt_out.write(json.dumps(asdict(receipt), separators=(",", ":")) + "\n")
            receipt_out.flush()
            summary.observations_written += len(observations)
            summary.by_status[receipt.status] = summary.by_status.get(receipt.status, 0) + 1
    return summary


def _with(receipt: ChunkReceipt, **changes: Any) -> ChunkReceipt:
    return ChunkReceipt(**{**asdict(receipt), **changes})


def run_pending(
    data_dir: Path, *, limit: int, max_seconds: float, get: GetJson | None = None,
    now: datetime | None = None, clock: Callable[[], float] = time.monotonic,
    priority: AbstractSet[ChunkKey] = frozenset(),
) -> dict[str, Any]:
    """One bounded pass for the chunks a data directory's buys reach (lean-pilot stage).

    ``priority`` chunks are fetched first (see :func:`select_pending`); the result
    counts those still without a final receipt, ended or not, and those selected.
    ``partial`` when the pass stopped early, a chunk failed, or the token lookup
    failed. Exception details stay in the log, never the result. Every pass
    compacts the observations before it fetches and after (``compacted_bytes``).
    """
    store = data_dir / STORE_DIR
    needed = needed_chunks(data_dir / ACTIVITY_FILE)
    receipts = latest_receipts(store)
    pending = select_pending(needed, receipts, now=now or datetime.now(UTC), limit=limit,
                             priority=priority)
    result: dict[str, Any] = {
        "chunks_needed": len(needed), "selected": len(pending),
        "priority_open": sum(key in needed and (key not in receipts
                                                or receipts[key][0] not in FINAL_STATUSES)
                             for key in priority),
        "priority_selected": sum(key in priority for key in pending),
    }
    # Compact first too: rows a crashed pass left behind, including a torn final
    # row, must not have this pass's rows appended straight after them.
    compacted = compact_observations(store)
    fetched = (_fetch(pending, store, get=get, max_seconds=max_seconds, clock=clock)
               if pending else {"status": "succeeded", "summary": asdict(BackfillSummary())})
    compacted += compact_observations(store)
    return {"status": fetched["status"], **result, **fetched, "compacted_bytes": compacted}


def _fetch(pending: list[ChunkKey], store: Path, *, get: GetJson | None, max_seconds: float,
           clock: Callable[[], float]) -> dict[str, Any]:
    deadline = clock() + max_seconds
    try:
        if get is None:
            with httpx.Client(timeout=20.0, headers=HEADERS) as client:
                summary = backfill(pending, store, get=http_get_json(client),
                                   deadline=deadline, clock=clock)
        else:
            summary = backfill(pending, store, get=get, deadline=deadline, clock=clock)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("entry-price token lookup failed: %s", exc)
        return {"status": "partial", "error_type": type(exc).__name__}
    failed = summary.by_status.get("http_error", 0) > 0
    status = "partial" if summary.stopped_early or failed else "succeeded"
    return {"status": status, "summary": asdict(summary)}


def compact_observations(store: Path) -> int:
    """Move the plain observation file's complete rows into the gzip archive and
    return how many bytes moved.

    The new archive (the old one's bytes plus one new gzip member) is written beside
    it, synced and swapped in; only then is the plain file removed. A crash before
    the swap leaves both files as they were. A crash after it leaves the rows in
    both, and the next compaction archives them again: duplicate rows, which every
    reader collapses to one point per timestamp. Bytes after the plain file's last
    newline are a torn row from a crash mid-append, written before its chunk's
    receipt; they are dropped and that chunk is fetched again.
    """
    plain = store / OBSERVATIONS_FILE
    if not plain.exists():
        return 0
    end = _complete_bytes(plain)
    if end < plain.stat().st_size:
        log.warning("dropping a torn final row from %s", plain.name)
    if end == 0:
        plain.unlink()
        return 0
    archive = store / ARCHIVE_FILE
    tmp = store / (ARCHIVE_FILE + ".tmp")
    try:
        with tmp.open("wb") as out:
            if archive.exists():
                with archive.open("rb") as old:
                    shutil.copyfileobj(old, out)
            with plain.open("rb") as src, gzip.GzipFile(
                    fileobj=out, mode="wb", compresslevel=6, mtime=0) as member:
                remaining = end
                while remaining > 0:
                    block = src.read(min(1 << 20, remaining))
                    if not block:
                        raise OSError(f"{plain.name} shrank while it was compacted")
                    member.write(block)
                    remaining -= len(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, archive)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    plain.unlink()
    return end


def _complete_bytes(path: Path) -> int:
    """The length of ``path`` up to and including its last newline."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        position = size
        while position > 0:
            step = min(1 << 16, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            newline = block.rfind(b"\n")
            if newline >= 0:
                return position + newline + 1
    return 0


# ── Reading, for the horizon diagnostic and later scoring ────────────────────

def load_entry_prices(
    store: Path, *, observed_before: datetime | None = None,
) -> dict[str, list[tuple[int, float]]]:
    """Sorted hourly YES series per condition id, one point per timestamp.

    With ``observed_before``, only rows fetched at or before it count, so a frozen
    snapshot never sees prices fetched later.
    """
    by_market: dict[str, dict[int, float]] = {}
    for row in _observation_rows(store):
        if row.get("outcome_index") != 0:
            continue
        observed = _parse_time(row.get("observed_time"))
        event = _parse_time(row.get("event_time"))
        price = row.get("price")
        if (observed is None or event is None or isinstance(price, bool)
                or not isinstance(price, (int, float)) or not 0.0 <= price <= 1.0):
            continue
        if observed_before is not None and observed > observed_before:
            continue
        cid = str(row.get("condition_id", "")).lower()
        by_market.setdefault(cid, {})[int(event.timestamp())] = float(price)
    return {cid: sorted(points.items()) for cid, points in by_market.items()}


def _observation_rows(store: Path) -> Iterator[dict[str, Any]]:
    """Every stored observation, oldest first: the archive, then the plain file."""
    archive = store / ARCHIVE_FILE
    if archive.exists():
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            yield from _parse_lines(handle, archive.name)
    yield from _read_jsonl(store / OBSERVATIONS_FILE)


def price_after(
    series: list[tuple[int, float]], buy_ts: int, horizon_seconds: int, *,
    tolerance_seconds: int = 2 * 3600,
) -> float | None:
    """YES price ``horizon_seconds`` after a buy: the last point at or before that
    moment, if it is within ``tolerance_seconds`` of it and after the buy. None when
    the series has no such point (a gap, or the market had closed)."""
    target = buy_ts + horizon_seconds
    index = bisect.bisect_right(series, (target, float("inf"))) - 1
    if index < 0:
        return None
    at, price = series[index]
    if at <= buy_ts or target - at > tolerance_seconds:
        return None
    return price
