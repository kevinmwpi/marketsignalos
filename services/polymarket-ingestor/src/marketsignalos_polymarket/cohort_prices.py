"""Stage 3 step 3: prices after the follower's entry, for each cohort-v1 signal.

Plan S3's primary outcome is the price of the side bought one hour after the
follower's entry (the signal's ``detected_at``); the 6 h horizon is a secondary.
For every signal with a token, captured or excluded (so the evaluation can check
that exclusions do not select on outcome), this fetches the bought token's
``/prices-history`` over ``[detected_at - WINDOW_BEFORE, detected_at + WINDOW_AFTER]``.

It reuses the entry-price backfill's rules, not its chunks:

- a window is fetched only once it has ended and ``SETTLE_SECONDS`` have passed, so
  its prices can no longer change, and a final window (``ok`` or ``empty``) is never
  fetched again; a failed one waits ``RETRY_AFTER``;
- rows follow the ``price_observations`` contract (event time and observed time),
  are written before their receipt, and a torn final row is cut before an append.

The backfill's 7-day chunks are fetched only after they end, so a signal near the
end of the window would have no price until days after the evaluation date. A
window per signal ends seven hours after detection.

Prices are those of the token bought, not YES. ``FIDELITY_MINUTES`` is requested;
each receipt records the points returned and their median spacing, so the burn-in
shows whether the API honours it before the outcome's tolerance is frozen.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from .closing_lines import (
    CLOB_BASE_URL,
    GetJson,
    _parse_time,
    _read_jsonl,
    _trim_torn_tail,
    http_get_json,
)

log = logging.getLogger("marketsignalos.polymarket.cohort_prices")

STAGE_DIR = "cohort-v1"
SIGNALS_FILE = "signals.jsonl"
OBSERVATIONS_FILE = "price_observations.jsonl"
RECEIPTS_FILE = "price_receipts.jsonl"
SOURCE = "clob:/prices-history"
FIDELITY_MINUTES = 5
WINDOW_BEFORE = 3600
WINDOW_AFTER = 6 * 3600  # the 6 h secondary horizon
SETTLE_SECONDS = 3600
FINAL_STATUSES = frozenset({"ok", "empty"})
RETRY_AFTER = timedelta(hours=1)
HEADERS = {"User-Agent": "MarketSignalOS-cohort-prices/0.1", "Accept": "application/json"}


@dataclass(frozen=True, slots=True)
class WindowReceipt:
    signal_id: str
    status: str  # ok | empty | http_error
    observed_time: str
    window_start: str
    window_end: str
    token_id: str
    points: int = 0
    median_spacing_seconds: float | None = None
    fidelity_minutes: int = FIDELITY_MINUTES
    error: str = ""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _signals(stage: Path) -> list[dict[str, Any]]:
    return [row for row in _read_jsonl(stage / SIGNALS_FILE)
            if row.get("token_id") and _parse_time(row.get("detected_at")) is not None]


def latest_receipts(stage: Path) -> dict[str, tuple[str, datetime]]:
    latest: dict[str, tuple[str, datetime]] = {}
    for row in _read_jsonl(stage / RECEIPTS_FILE):
        observed = _parse_time(row.get("observed_time"))
        if observed is None:
            continue
        sid = str(row.get("signal_id", ""))
        if sid not in latest or observed >= latest[sid][1]:
            latest[sid] = (str(row.get("status", "")), observed)
    return latest


def due(signals: list[dict[str, Any]], receipts: dict[str, tuple[str, datetime]], *,
        now: datetime) -> list[dict[str, Any]]:
    """Signals whose window has ended and settled and has no final receipt, oldest
    first; a failed window waits ``RETRY_AFTER``."""
    ready: list[dict[str, Any]] = []
    for signal in signals:
        detected = _parse_time(signal["detected_at"])
        assert detected is not None
        if detected + timedelta(seconds=WINDOW_AFTER + SETTLE_SECONDS) > now:
            continue
        receipt = receipts.get(str(signal["signal_id"]))
        if receipt is not None and (receipt[0] in FINAL_STATUSES
                                    or now - receipt[1] < RETRY_AFTER):
            continue
        ready.append(signal)
    return sorted(ready, key=lambda s: (str(s["detected_at"]), str(s["signal_id"])))


def fetch_window(token_id: str, start: int, end: int, get: GetJson) -> list[tuple[int, float]]:
    payload = get(f"{CLOB_BASE_URL}/prices-history", {
        "market": token_id, "startTs": start, "endTs": end, "fidelity": FIDELITY_MINUTES})
    rows = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("CLOB /prices-history: expected {'history': [...]}")
    return sorted((int(row["t"]), float(row["p"])) for row in rows
                  if isinstance(row, dict) and isinstance(row.get("t"), (int, float))
                  and isinstance(row.get("p"), (int, float)) and start <= row["t"] <= end
                  and 0.0 <= row["p"] <= 1.0)


def _median_spacing(points: list[tuple[int, float]]) -> float | None:
    gaps = [b[0] - a[0] for a, b in pairwise(points)]
    return float(statistics.median(gaps)) if gaps else None


def run_pending(data_dir: Path, *, max_seconds: float, get: GetJson | None = None,
                now: datetime | None = None,
                clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One bounded pass over the signals whose windows are due. Counts only."""
    stage = data_dir / STAGE_DIR
    pending = due(_signals(stage), latest_receipts(stage), now=now or datetime.now(UTC))
    result: dict[str, Any] = {"status": "succeeded", "due": len(pending), "by_status": {},
                              "observations_written": 0, "stopped_early": False}
    if not pending:
        return result
    if get is None:
        with httpx.Client(timeout=20.0, headers=HEADERS) as client:
            return _fetch_all(stage, pending, result, http_get_json(client), max_seconds, clock)
    return _fetch_all(stage, pending, result, get, max_seconds, clock)


def _fetch_all(stage: Path, pending: list[dict[str, Any]], result: dict[str, Any],
               get: GetJson, max_seconds: float,
               clock: Callable[[], float]) -> dict[str, Any]:
    deadline = clock() + max_seconds
    observations, receipts = stage / OBSERVATIONS_FILE, stage / RECEIPTS_FILE
    _trim_torn_tail(observations)
    _trim_torn_tail(receipts)
    by_status: dict[str, int] = {}
    for signal in pending:
        if clock() > deadline:
            result["stopped_early"] = True
            break
        detected = _parse_time(signal["detected_at"])
        assert detected is not None
        start = int(detected.timestamp()) - WINDOW_BEFORE
        end = int(detected.timestamp()) + WINDOW_AFTER
        token = str(signal["token_id"])
        sid = str(signal["signal_id"])
        try:
            points = fetch_window(token, start, end, get)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("cohort prices: window fetch failed: %s", type(exc).__name__)
            receipt = WindowReceipt(sid, "http_error", datetime.now(UTC).isoformat(),
                                    _iso(start), _iso(end), token, error=type(exc).__name__)
        else:
            observed = datetime.now(UTC).isoformat()
            with observations.open("a", encoding="utf-8") as out:
                for at, price in points:
                    out.write(json.dumps({
                        "signal_id": sid, "condition_id": signal["condition_id"],
                        "token_id": token, "outcome_index": signal["outcome_index"],
                        "event_time": _iso(at), "observed_time": observed, "price": price,
                        "source": SOURCE, "fidelity_minutes": FIDELITY_MINUTES,
                    }, separators=(",", ":")) + "\n")
            result["observations_written"] += len(points)
            receipt = WindowReceipt(sid, "ok" if points else "empty", observed, _iso(start),
                                    _iso(end), token, points=len(points),
                                    median_spacing_seconds=_median_spacing(points))
        with receipts.open("a", encoding="utf-8") as out:
            out.write(json.dumps(asdict(receipt), separators=(",", ":")) + "\n")
        by_status[receipt.status] = by_status.get(receipt.status, 0) + 1
    result["by_status"] = dict(sorted(by_status.items()))
    spacings = [float(r["median_spacing_seconds"]) for r in _read_jsonl(receipts)
                if isinstance(r.get("median_spacing_seconds"), (int, float))]
    result["median_spacing_seconds"] = statistics.median(spacings) if spacings else None
    if result["stopped_early"] or by_status.get("http_error"):
        result["status"] = "partial"
    return result


def load_signal_prices(data_dir: Path) -> dict[str, list[tuple[int, float]]]:
    """Sorted price series of the token bought, per signal id (the evaluation's input)."""
    series: dict[str, dict[int, float]] = {}
    for row in _read_jsonl(data_dir / STAGE_DIR / OBSERVATIONS_FILE):
        event = _parse_time(row.get("event_time"))
        price = row.get("price")
        if event is None or isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        series.setdefault(str(row.get("signal_id", "")), {})[int(event.timestamp())] = float(price)
    return {sid: sorted(points.items()) for sid, points in series.items()}
