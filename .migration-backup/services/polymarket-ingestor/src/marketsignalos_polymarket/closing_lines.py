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
Nothing here feeds scoring yet; using these closing lines for CLV is a separate,
versioned change.

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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
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
# A market in one of these states is never fetched again. Everything else
# (not yet closed, missing fields, HTTP errors) is retried on the next run.
FINAL_STATUSES = frozenset({"ok", "no_history"})

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


def http_get_json(client: httpx.Client, *, spacing_seconds: float = 0.2) -> GetJson:
    """Default ``GetJson`` over one shared client, politely spaced."""

    def get(url: str, params: dict[str, Any]) -> Any:
        time.sleep(spacing_seconds)
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    return get


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield rows; tolerate only a torn final line (a crash mid-append), nothing else."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    complete, trailing = lines[:-1], lines[-1]
    for number, line in enumerate(complete, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{number}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number}: expected an object")
        yield row
    if trailing.strip():
        log.warning("ignoring a torn final line in %s", path.name)


def final_conditions(store: Path) -> set[str]:
    """Condition ids whose latest receipt is final."""
    latest: dict[str, str] = {}
    for row in _read_jsonl(store / RECEIPTS_FILE):
        latest[str(row.get("condition_id", "")).lower()] = str(row.get("status", ""))
    return {cid for cid, status in latest.items() if status in FINAL_STATUSES}


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
) -> BackfillSummary:
    """Fetch pre-close windows for resolved markets not already final in ``store``.

    Each market's observations are appended before its receipt, so an ``ok``
    receipt always has its rows. A crash between the two refetches that market
    next run; the repeat rows differ only in ``observed_time``, and readers keep
    the latest observation per (token, event_time).
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
            receipt, observations = _receipt_for(cid, rows.get(cid), get, window_hours)
            for observation in observations:
                obs_out.write(json.dumps(asdict(observation), separators=(",", ":")) + "\n")
            obs_out.flush()
            receipt_out.write(json.dumps(asdict(receipt), separators=(",", ":")) + "\n")
            receipt_out.flush()
            summary.observations_written += len(observations)
            summary.by_status[receipt.status] = summary.by_status.get(receipt.status, 0) + 1
    return summary


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
    headers = {"User-Agent": "MarketSignalOS-closing-lines/0.1", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
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
