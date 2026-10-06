"""
Backfill each resolved market's pre-close price path from Polymarket's CLOB.

Closing-line value (CLV) needs a market's price just before it closed. The live
probe in docs/benchmarks/2026-09-29-price-history-probe.md established:

  - ``/prices-history`` with explicit ``startTs``/``endTs`` returns 1-hour points
    for every probed market that closed in 2023 or later, the last one at most
    1.5 h before close;
  - ``interval=max`` returns nothing finer than 12 hours for the same markets, so
    this module never sends ``interval``;
  - markets from 2021-2022 return no points (pre-order-book era) and are recorded
    as ``no_history``, a final answer rather than a failure to retry.

Rows follow the blueprint's ``price_observations`` contract (docs/handoff-blueprint.md
section 8): each point keeps its event time and the time it was fetched, so a
backfilled price can never pass for one observed live. Both files are append-only.

Scoring does not read this store. The last pre-close price was within 0.01 of the
outcome for 38 of 40 probed markets, so CLV against it would mostly restate whether
a bet won; which pre-close point should serve as the closing line is an open gate-13
decision (docs/handoff-blueprint.md). :func:`load_closing_lines` is the reader that
decision will build on; any use in scoring must pass ``observed_before`` when
rescoring a frozen snapshot, so later backfills cannot leak into it. The lean-pilot
worker fills the store in bounded batches through :func:`run_pending`, so the
windows exist once the definition is settled.

The YES price is the first outcome's token, matching how CLV already reads prices.

CLI (run from the repository root)::

    python -m marketsignalos_polymarket.closing_lines \\
        --condition-ids-file ids.txt --store services/ingestor/data/closing_lines
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .market_rules import GAMMA_BASE_URL

log = logging.getLogger("marketsignalos.polymarket.closing_lines")

CLOB_BASE_URL = "https://clob.polymarket.com"
SOURCE = "clob:/prices-history"
FIDELITY_MINUTES = 60
DEFAULT_WINDOW_HOURS = 48
GAMMA_BATCH = 20
OBSERVATIONS_FILE = "price_observations.jsonl"
RECEIPTS_FILE = "closing_line_receipts.jsonl"
# The store's directory inside a pilot/ingestor data directory.
STORE_DIR = "closing_lines"
ACTIVITY_FILE = "polymarket_activity.jsonl"
# A market in one of these states is never fetched again. Everything else
# (not yet closed, missing fields, HTTP errors) is retried, but the worker waits
# RETRY_AFTER before asking again so open markets do not eat every batch.
FINAL_STATUSES = frozenset({"ok", "no_history"})
RETRY_AFTER = timedelta(hours=24)
HEADERS = {"User-Agent": "MarketSignalOS-closing-lines/0.1", "Accept": "application/json"}

# (url, params) -> parsed JSON; transport and HTTP errors raise httpx.HTTPError.
GetJson = Callable[[str, dict[str, Any]], Any]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_token_ids(raw: Any) -> list[str]:
    """Gamma returns ``clobTokenIds`` as a JSON-encoded string list, sometimes a list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [str(token) for token in raw if str(token).strip()]


def parse_close(row: dict[str, Any]) -> tuple[int | None, str]:
    """The actual close time when Gamma has it, else the scheduled end date."""
    for name in ("closedTime", "endDate"):
        value = row.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp()), name
    return None, ""


@dataclass(frozen=True, slots=True)
class PriceObservation:
    condition_id: str
    token_id: str
    outcome_index: int
    event_time: str  # when the price was true
    observed_time: str  # when this row was fetched
    price: float
    fidelity_minutes: int
    source: str = SOURCE


@dataclass(frozen=True, slots=True)
class ClosingLineReceipt:
    condition_id: str
    status: str  # ok | no_history | not_closed | no_token | no_close_time | http_error
    observed_time: str
    token_id: str = ""
    close_time: str = ""
    close_field: str = ""
    window_start: str = ""
    window_end: str = ""
    points: int = 0
    last_point_time: str = ""
    hours_last_point_before_close: float | None = None
    fidelity_minutes: int = FIDELITY_MINUTES
    error: str = ""


@dataclass(slots=True)
class BackfillSummary:
    requested: int = 0
    skipped_final: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    observations_written: int = 0
    stopped_early: bool = False


@dataclass(frozen=True, slots=True)
class ClosingLines:
    """A point-in-time summary of the store.

    ``points`` maps a condition id to its latest pre-close YES observation
    (event time, price). ``no_history`` holds markets whose latest receipt says
    the order book has no price history at all (the 2021-2022 era); CLV must
    exclude those with that reason rather than count them as missing.
    """

    points: dict[str, tuple[datetime, float]] = field(default_factory=dict)
    no_history: frozenset[str] = frozenset()


def http_get_json(client: httpx.Client, *, spacing_seconds: float = 0.2) -> GetJson:
    """Default ``GetJson`` over one shared client, politely spaced."""

    def get(url: str, params: dict[str, Any]) -> Any:
        time.sleep(spacing_seconds)
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    return get


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream rows; tolerate only a torn final line (a crash mid-append), nothing else.

    Every writer ends a row with a newline, so a last line without one is torn
    even if it happens to parse.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        yield from _parse_lines(handle, path.name)


def _parse_lines(lines: Iterable[str], name: str) -> Iterator[dict[str, Any]]:
    """The rows of an open JSONL stream, by :func:`_read_jsonl`'s rules."""
    for number, line in enumerate(lines, 1):
        if not line.endswith("\n"):
            if line.strip():
                log.warning("ignoring a torn final line in %s", name)
            return
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}:{number}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{name}:{number}: expected an object")
        yield row


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def latest_receipts(
    store: Path, *, observed_before: datetime | None = None,
) -> dict[str, tuple[str, datetime]]:
    """Latest (status, observed time) per condition id, optionally as of a cutoff."""
    latest: dict[str, tuple[str, datetime]] = {}
    for row in _read_jsonl(store / RECEIPTS_FILE):
        observed = _parse_time(row.get("observed_time"))
        if observed is None or (observed_before is not None and observed > observed_before):
            continue
        cid = str(row.get("condition_id", "")).lower()
        if cid:
            latest[cid] = (str(row.get("status", "")), observed)
    return latest


def close_times(store: Path) -> dict[str, int]:
    """Actual close time (Unix seconds) per market, from the latest receipt whose
    close came from Gamma's ``closedTime`` rather than the scheduled end date."""
    found: dict[str, int] = {}
    for row in _read_jsonl(store / RECEIPTS_FILE):
        closed = _parse_time(row.get("close_time"))
        cid = str(row.get("condition_id", "")).lower()
        if cid and closed is not None and row.get("close_field") == "closedTime":
            found[cid] = int(closed.timestamp())
    return found


def final_conditions(store: Path) -> set[str]:
    """Condition ids whose latest receipt is final."""
    return {cid for cid, (status, _) in latest_receipts(store).items()
            if status in FINAL_STATUSES}


def load_closing_lines(store: Path, *, observed_before: datetime | None = None) -> ClosingLines:
    """The latest pre-close YES price per market and the markets with no history.

    Only rows fetched at or before ``observed_before`` count when it is given: a
    frozen snapshot rescored later must not see prices backfilled after it. Memory
    is one point per market however many observations the store holds.
    """
    receipts = latest_receipts(store, observed_before=observed_before)
    no_history = frozenset(cid for cid, (status, _) in receipts.items()
                           if status == "no_history")
    points: dict[str, tuple[datetime, float]] = {}
    for row in _read_jsonl(store / OBSERVATIONS_FILE):
        if row.get("outcome_index") != 0:
            continue
        observed = _parse_time(row.get("observed_time"))
        event = _parse_time(row.get("event_time"))
        price = row.get("price")
        if (observed is None or event is None or isinstance(price, bool)
                or not isinstance(price, (int, float)) or price <= 0.0):
            continue
        if observed_before is not None and observed > observed_before:
            continue
        cid = str(row.get("condition_id", "")).lower()
        current = points.get(cid)
        if current is None or event >= current[0]:
            points[cid] = (event, float(price))
    return ClosingLines(points=points, no_history=no_history)


def activity_condition_ids(path: Path) -> list[str]:
    """Distinct condition ids of TRADE events, in first-seen order.

    Reads the activity store the way the ingestor does: row by row, skipping
    lines it cannot parse, because that file can exceed memory.
    """
    seen: dict[str, None] = {}
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("type") == "TRADE":
                cid = str(row.get("condition_id", "")).strip().lower()
                if cid:
                    seen.setdefault(cid, None)
    return list(seen)


def select_pending(
    condition_ids: Iterable[str], receipts: dict[str, tuple[str, datetime]], *,
    now: datetime, limit: int, retry_after: timedelta = RETRY_AFTER,
    priority: AbstractSet[str] = frozenset(),
) -> list[str]:
    """Markets to fetch next: never-tried ones first, then the stalest retries, with
    every ``priority`` market ahead of every other one in the same order.

    Final markets are skipped for good. A market that was open, or whose fetch
    failed, waits ``retry_after`` before it is asked about again.
    """
    fresh: list[str] = []
    retry: list[tuple[datetime, str]] = []
    for cid in dict.fromkeys(c.strip().lower() for c in condition_ids if c.strip()):
        receipt = receipts.get(cid)
        if receipt is None:
            fresh.append(cid)
        elif receipt[0] not in FINAL_STATUSES and now - receipt[1] >= retry_after:
            retry.append((receipt[1], cid))
    retry.sort()
    ordered = fresh + [cid for _, cid in retry]
    ordered = ([cid for cid in ordered if cid in priority]
               + [cid for cid in ordered if cid not in priority])
    return ordered[:max(0, limit)]


def lookup_closed_markets(condition_ids: list[str], get: GetJson) -> dict[str, dict[str, Any]]:
    """Gamma rows for the ids that are closed, keyed by lower-case condition id."""
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(condition_ids), GAMMA_BATCH):
        batch = condition_ids[start:start + GAMMA_BATCH]
        rows = get(f"{GAMMA_BASE_URL}/markets",
                   {"condition_ids": batch, "closed": "true", "limit": 100})
        if not isinstance(rows, list):
            raise ValueError(f"Gamma /markets: expected a list, saw {type(rows).__name__}")
        wanted = {cid.lower() for cid in batch}
        for row in rows:
            if isinstance(row, dict):
                cid = str(row.get("conditionId", "")).lower()
                if cid in wanted:
                    found[cid] = row
    return found


def fetch_window(
    token_id: str, close_ts: int, get: GetJson, *, window_hours: int = DEFAULT_WINDOW_HOURS,
) -> list[tuple[int, float]]:
    """1-hour points from ``window_hours`` before close up to close, sorted by time.

    Always an explicit window: ``interval=max`` caps resolved markets at 12 hours.
    Points after close are dropped; they are resolution payoffs, not market opinion.
    """
    payload = get(f"{CLOB_BASE_URL}/prices-history", {
        "market": token_id, "startTs": close_ts - window_hours * 3600,
        "endTs": close_ts, "fidelity": FIDELITY_MINUTES,
    })
    rows = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("CLOB /prices-history: expected {'history': [...]}")
    points: list[tuple[int, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t, p = row.get("t"), row.get("p")
        if isinstance(t, (int, float)) and isinstance(p, (int, float)) and t <= close_ts:
            points.append((int(t), float(p)))
    return sorted(points)


def _receipt_for(
    cid: str, row: dict[str, Any] | None, get: GetJson, window_hours: int,
) -> tuple[ClosingLineReceipt, list[PriceObservation]]:
    observed = _utcnow_iso()
    if row is None:
        return ClosingLineReceipt(cid, "not_closed", observed), []
    tokens = parse_token_ids(row.get("clobTokenIds"))
    if not tokens:
        return ClosingLineReceipt(cid, "no_token", observed), []
    close_ts, close_field = parse_close(row)
    if close_ts is None:
        return ClosingLineReceipt(cid, "no_close_time", observed, token_id=tokens[0]), []
    window = ClosingLineReceipt(
        cid, "", observed, token_id=tokens[0], close_time=_iso(close_ts),
        close_field=close_field, window_start=_iso(close_ts - window_hours * 3600),
        window_end=_iso(close_ts),
    )
    try:
        points = fetch_window(tokens[0], close_ts, get, window_hours=window_hours)
    except (httpx.HTTPError, ValueError) as exc:
        return replace(window, status="http_error", error=str(exc)[:200]), []
    if not points:
        return replace(window, status="no_history"), []
    last_t = points[-1][0]
    receipt = replace(
        window, status="ok", points=len(points), last_point_time=_iso(last_t),
        hours_last_point_before_close=round((close_ts - last_t) / 3600, 2),
    )
    observations = [
        PriceObservation(cid, tokens[0], 0, _iso(t), observed, p, FIDELITY_MINUTES)
        for t, p in points
    ]
    return receipt, observations


def backfill(
    condition_ids: Iterable[str], store: Path, *, get: GetJson,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
) -> BackfillSummary:
    """Fetch pre-close windows for resolved markets not already final in ``store``.

    Each market's observations are appended before its receipt, so an ``ok``
    receipt always has its rows. A crash between the two refetches that market
    next run; the repeat rows differ only in ``observed_time``, and readers keep
    the latest observation per (token, event_time). With a ``deadline`` (on
    ``clock``), no new market is started once it has passed; markets left over
    have no receipt and are picked up by the next run.
    """
    wanted = list(dict.fromkeys(cid.strip().lower() for cid in condition_ids if cid.strip()))
    summary = BackfillSummary(requested=len(wanted))
    store.mkdir(parents=True, exist_ok=True)
    done = final_conditions(store)
    todo = [cid for cid in wanted if cid not in done]
    summary.skipped_final = len(wanted) - len(todo)
    if not todo:
        return summary
    rows = lookup_closed_markets(todo, get)
    with (store / OBSERVATIONS_FILE).open("a", encoding="utf-8") as obs_out, \
            (store / RECEIPTS_FILE).open("a", encoding="utf-8") as receipt_out:
        for cid in todo:
            if deadline is not None and clock() >= deadline:
                summary.stopped_early = True
                break
            receipt, observations = _receipt_for(cid, rows.get(cid), get, window_hours)
            for observation in observations:
                obs_out.write(json.dumps(asdict(observation), separators=(",", ":")) + "\n")
            obs_out.flush()
            receipt_out.write(json.dumps(asdict(receipt), separators=(",", ":")) + "\n")
            receipt_out.flush()
            summary.observations_written += len(observations)
            summary.by_status[receipt.status] = summary.by_status.get(receipt.status, 0) + 1
    return summary


def run_pending(
    data_dir: Path, *, limit: int, max_seconds: float, get: GetJson | None = None,
    now: datetime | None = None, clock: Callable[[], float] = time.monotonic,
    priority: AbstractSet[str] = frozenset(),
) -> dict[str, Any]:
    """One bounded backfill pass for the markets a data directory's wallets traded.

    Used by the lean-pilot worker. At most ``limit`` markets are attempted and no
    new one starts after ``max_seconds``; the rest wait for the next pass.
    ``priority`` markets go first (see :func:`select_pending`); the result counts
    those without a final receipt and those selected. The result is ``partial`` when
    the pass stopped early, a market's fetch failed, or the Gamma lookup failed.
    Exception details stay in the log, never the result.
    """
    store = data_dir / STORE_DIR
    conditions = activity_condition_ids(data_dir / ACTIVITY_FILE)
    receipts = latest_receipts(store)
    pending = select_pending(conditions, receipts, now=now or datetime.now(UTC),
                             limit=limit, priority=priority)
    traded = set(conditions)
    result: dict[str, Any] = {
        "conditions_in_activity": len(conditions), "selected": len(pending),
        "priority_open": sum(cid in traded and (cid not in receipts
                                                or receipts[cid][0] not in FINAL_STATUSES)
                             for cid in priority),
        "priority_selected": sum(cid in priority for cid in pending),
    }
    if not pending:
        return {"status": "succeeded", **result, "summary": asdict(BackfillSummary())}
    deadline = clock() + max_seconds
    try:
        if get is None:
            with httpx.Client(timeout=20.0, headers=HEADERS) as client:
                summary = backfill(pending, store, get=http_get_json(client),
                                   deadline=deadline, clock=clock)
        else:
            summary = backfill(pending, store, get=get, deadline=deadline, clock=clock)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("closing-line market lookup failed: %s", exc)
        return {"status": "partial", **result, "error_type": type(exc).__name__}
    failed = summary.by_status.get("http_error", 0) > 0
    status = "partial" if summary.stopped_early or failed else "succeeded"
    return {"status": status, **result, "summary": asdict(summary)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m marketsignalos_polymarket.closing_lines")
    parser.add_argument("--condition-ids-file", type=Path, required=True,
                        help="One condition id per line")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    if args.window_hours < 1:
        parser.error("--window-hours must be positive")
    ids = [line.strip() for line in args.condition_ids_file.read_text().splitlines()]
    ids = [cid for cid in ids if cid][: args.limit]
    try:
        with httpx.Client(timeout=20.0, headers=HEADERS) as client:
            summary = backfill(ids, args.store, get=http_get_json(client),
                               window_hours=args.window_hours)
    except (httpx.HTTPError, ValueError) as exc:
        log.error("closing-line backfill stopped: %s", exc)
        return 1
    log.info("closing_lines %s", json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
