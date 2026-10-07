"""Stage 3 step 2: record what a follower could have paid for each cohort-v1 signal.

A signal is a cohort member's BUY of one outcome, seen for the first time by a
pilot collection. The fills one collection sees for the same (wallet, market,
outcome) are one signal: a follower acts once per run. For each one, in the same
run that saw it:

- the order book for the token bought (``GET clob /book``): best bid and ask, and a
  ``CLIP_USDC`` clip walked through the asks, so slippage is measured;
- the market's taker fee (``GET clob /clob-markets/{condition}``, field ``fd``:
  rate ``r``, exponent ``e``). Polymarket's own client (clob-client-v2,
  ``adjustBuyAmountForFees``) charges ``shares * r * (p * (1 - p)) ** e`` in USDC on
  top of a buy, and charges nothing when ``fd`` is absent; this module does the
  same at each level's price;
- Gamma's ``feesEnabled``, ``feeSchedule`` and ``clobTokenIds`` for the same
  market, as cross-checks of the fee and of the token bought (activity rows carry
  only the outcome index, so a wrong token would price the other side).

A signal with no book, no fee details, fee or token sources that disagree, too little
depth for the clip, or a market not accepting orders is written with its reasons and is
excluded from the evaluation, never priced at zero cost (plan S4). Fills older than
``MAX_DETECTION_LAG`` when seen (a wallet's first poll, or a run outage) are not
signals: the follower would not have been following in real time. They are counted.

Only rows appended during the current collection are read, from a byte offset taken
before it, so the archive is never rescanned. Rows go to ``cohort-v1/signals.jsonl``
with the cohort ID and config hash of the member list in force. Logs carry counts
only, never prices or performance (plan step 2).
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .closing_lines import (
    CLOB_BASE_URL,
    GetJson,
    _trim_torn_tail,
    http_get_json,
    parse_token_ids,
)
from .market_rules import GAMMA_BASE_URL

log = logging.getLogger("marketsignalos.polymarket.cohort_capture")

STAGE_DIR = "cohort-v1"
SIGNALS_FILE = "signals.jsonl"
ACTIVITY_FILE = "polymarket_activity.jsonl"
CLIP_USDC = 100.0
MAX_DETECTION_LAG_SECONDS = 3 * 3600  # one skipped hourly run still counts
MAX_SECONDS = 120.0
GAMMA_BATCH = 20
HEADERS = {"User-Agent": "MarketSignalOS-cohort-capture/0.1", "Accept": "application/json"}
FEE_SOURCE = "clob:/clob-markets fd (r, e); fee = shares * r * (p * (1 - p)) ** e per level"


def activity_offset(data_dir: Path) -> int:
    """Bytes in the plain activity file now: rows after this are this run's."""
    try:
        return (data_dir / ACTIVITY_FILE).stat().st_size
    except FileNotFoundError:
        return 0


def appended_rows(data_dir: Path, offset: int) -> list[dict[str, Any]]:
    """Activity rows written after ``offset``. A partial first line is skipped (the
    offset fell inside a row), as is a torn last line."""
    path = data_dir / ACTIVITY_FILE
    if not path.exists():
        return []
    with path.open("rb") as handle:
        start = max(0, offset)
        if start:
            handle.seek(start - 1)
            aligned = handle.read(1) == b"\n"
        else:
            aligned = True
        handle.seek(start)
        raw = handle.read()
    lines = raw.split(b"\n")
    if not aligned:
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for line in lines[:-1]:  # the last piece is empty, or a torn row
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def walk_asks(asks: list[tuple[float, float]], clip_usdc: float, *, rate: float,
              exponent: float) -> dict[str, Any] | None:
    """Buy ``clip_usdc`` of notional up the ask side; the fee comes on top.

    None when the book cannot fill the clip.
    """
    remaining = clip_usdc
    shares = fee = 0.0
    levels = 0
    for price, size in sorted(asks):
        if remaining <= 1e-9:
            break
        if not 0.0 < price < 1.0 or size <= 0.0:
            continue
        take = min(remaining, price * size)
        bought = take / price
        shares += bought
        fee += taker_fee(bought, price, rate=rate, exponent=exponent)
        remaining -= take
        levels += 1
    if remaining > 1e-9 or shares <= 0.0:
        return None
    return {"shares": round(shares, 6), "vwap": round(clip_usdc / shares, 6),
            "fee_usdc": round(fee, 6), "all_in_price": round((clip_usdc + fee) / shares, 6),
            "levels": levels}


def taker_fee(shares: float, price: float, *, rate: float, exponent: float) -> float:
    """USDC fee for a taker buy, as Polymarket's clob-client-v2 computes it."""
    if rate <= 0.0:
        return 0.0
    return shares * rate * float((price * (1.0 - price)) ** exponent)


def _levels(raw: Any) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for level in raw if isinstance(raw, list) else []:
        if not isinstance(level, dict):
            continue
        try:
            levels.append((float(level["price"]), float(level["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return levels


def _signals(rows: list[dict[str, Any]], roster: dict[str, Any], *,
             now: datetime) -> tuple[list[dict[str, Any]], int]:
    """Fresh member BUY fills grouped by (wallet, market, outcome); and the number
    of fills too old to be followed."""
    sets = {name: {str(w).lower() for w in roster.get(name, [])}
            for name in ("t2", "comparison", "clv_only")}
    members = {str(w).lower() for w in roster.get("wallets", [])}
    groups: dict[tuple[str, str, int], dict[str, Any]] = {}
    stale = 0
    for row in rows:
        wallet = str(row.get("proxy_wallet", "")).lower()
        if (wallet not in members or row.get("type") != "TRADE"
                or str(row.get("side", "")).upper() != "BUY"):
            continue
        try:
            ts = int(row["timestamp"])
            index = int(row["outcome_index"])
            usdc = float(row.get("usdc_size") or 0.0)
            size = float(row.get("size") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if now.timestamp() - ts > MAX_DETECTION_LAG_SECONDS:
            stale += 1
            continue
        cid = str(row.get("condition_id", "")).lower()
        group = groups.setdefault((wallet, cid, index), {
            "wallet": wallet, "groups": [name for name, found in sets.items() if wallet in found],
            "condition_id": cid, "outcome_index": index,
            "event_slug": str(row.get("event_slug", "")), "fills": 0, "wallet_usdc": 0.0,
            "wallet_shares": 0.0, "first_fill_ts": ts, "last_fill_ts": ts})
        group["fills"] += 1
        group["wallet_usdc"] += usdc
        group["wallet_shares"] += size
        group["first_fill_ts"] = min(group["first_fill_ts"], ts)
        group["last_fill_ts"] = max(group["last_fill_ts"], ts)
    for group in groups.values():
        group["wallet_usdc"] = round(group["wallet_usdc"], 6)
        group["wallet_shares"] = round(group["wallet_shares"], 6)
    ordered = sorted(groups.values(), key=lambda g: (g["last_fill_ts"], g["wallet"],
                                                     g["condition_id"], g["outcome_index"]))
    return ordered, stale


def _gamma_fees(condition_ids: list[str], get: GetJson) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(condition_ids), GAMMA_BATCH):
        batch = condition_ids[start:start + GAMMA_BATCH]
        rows = get(f"{GAMMA_BASE_URL}/markets", {"condition_ids": batch, "limit": 100})
        if not isinstance(rows, list):
            raise ValueError("Gamma /markets: expected a list")
        for row in rows:
            if isinstance(row, dict) and str(row.get("conditionId", "")).lower() in batch:
                found[str(row["conditionId"]).lower()] = {
                    "fees_enabled": row.get("feesEnabled"),
                    "fee_schedule": row.get("feeSchedule"),
                    "token_ids": parse_token_ids(row.get("clobTokenIds"))}
    return found


def _market(cid: str, get: GetJson) -> dict[str, Any]:
    details = get(f"{CLOB_BASE_URL}/clob-markets/{cid}", {})
    if not isinstance(details, dict) or str(details.get("c", "")).lower() != cid:
        raise ValueError("CLOB /clob-markets: unexpected response")
    return details


def _capture_one(signal: dict[str, Any], details: dict[str, Any] | None,
                 gamma: dict[str, Any] | None, get: GetJson,
                 books: dict[str, Any], now: datetime) -> dict[str, Any]:
    reasons: list[str] = []
    row: dict[str, Any] = {**signal, "detected_at": now.isoformat(),
                           "detection_lag_seconds": int(now.timestamp()) - signal["last_fill_ts"],
                           "clip_usdc": CLIP_USDC}
    if details is None:
        row["exclusion_reasons"] = ["no fee details: CLOB market lookup failed"]
        return row
    fd = details.get("fd") if isinstance(details.get("fd"), dict) else None
    try:
        rate = float(fd.get("r") or 0.0) if fd else 0.0
        exponent = float(fd.get("e") or 0.0) if fd else 0.0
    except (TypeError, ValueError):
        row["exclusion_reasons"] = ["no fee details: unreadable fd"]
        return row
    gamma_enabled = gamma.get("fees_enabled") if gamma else None
    row["fee"] = {"source": FEE_SOURCE, "details_present": fd is not None, "rate": rate,
                  "exponent": exponent, "taker_only": fd.get("to") if fd else None,
                  "gamma_fees_enabled": gamma_enabled,
                  "gamma_fee_schedule": gamma.get("fee_schedule") if gamma else None}
    if isinstance(gamma_enabled, bool) and gamma_enabled != (rate > 0.0):
        reasons.append("fee sources disagree: Gamma feesEnabled vs CLOB fd")
    row["market"] = {key: details.get(key) for key in ("ao", "nr", "sd", "itode", "cbos", "mts")}
    if details.get("ao") is False:
        reasons.append("market not accepting orders")
    raw_tokens = details.get("t")
    tokens: list[Any] = raw_tokens if isinstance(raw_tokens, list) else []
    index = signal["outcome_index"]
    token = tokens[index] if 0 <= index < len(tokens) and isinstance(tokens[index], dict) else {}
    token_id = str(token.get("t", ""))
    gamma_tokens = gamma.get("token_ids") if gamma else None
    row["token_id"] = token_id
    row["token_outcome"] = token.get("o")
    if not token_id:
        reasons.append("no token for the outcome bought")
    elif gamma_tokens and (index >= len(gamma_tokens) or gamma_tokens[index] != token_id):
        reasons.append("token sources disagree: Gamma clobTokenIds vs CLOB t")
    if token_id and not reasons:
        if token_id not in books:
            try:
                books[token_id] = get(f"{CLOB_BASE_URL}/book", {"token_id": token_id})
            except (httpx.HTTPError, ValueError) as exc:
                books[token_id] = {"error_type": type(exc).__name__}
        book = books[token_id]
        if not isinstance(book, dict) or "error_type" in book:
            reasons.append("no book: request failed")
        else:
            asks, bids = _levels(book.get("asks")), _levels(book.get("bids"))
            row["book"] = {"timestamp": book.get("timestamp"), "hash": book.get("hash"),
                           "best_ask": min((p for p, _ in asks), default=None),
                           "best_bid": max((p for p, _ in bids), default=None),
                           "ask_depth_usdc": round(sum(p * s for p, s in asks), 6),
                           "tick_size": book.get("tick_size")}
            if not asks:
                reasons.append("no book: no asks")
            else:
                clip = walk_asks(asks, CLIP_USDC, rate=rate, exponent=exponent)
                if clip is None:
                    reasons.append("book too thin for the clip")
                else:
                    row["clip"] = clip
    row["exclusion_reasons"] = reasons
    return row


def capture(data_dir: Path, offset: int, run_id: str, *, get: GetJson | None = None,
            now_fn: Callable[[], datetime] | None = None,
            monotonic: Callable[[], float] = time.monotonic,
            max_seconds: float = MAX_SECONDS) -> dict[str, Any]:
    """Record this run's new member signals. Returns counts only."""
    now_fn = now_fn or (lambda: datetime.now(UTC))
    stage = data_dir / STAGE_DIR
    try:
        roster = json.loads((stage / "members.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "skipped", "reason": "no member list"}
    if not isinstance(roster, dict) or not roster.get("wallets"):
        return {"status": "skipped", "reason": "no members"}
    if activity_offset(data_dir) < offset:  # collection only appends; never guess
        return {"status": "skipped", "reason": "activity file shrank during collection"}
    signals, stale = _signals(appended_rows(data_dir, offset), roster, now=now_fn())
    result: dict[str, Any] = {"status": "succeeded", "signals": len(signals),
                              "stale_fills": stale, "captured": 0, "excluded": {}}
    if not signals:
        return result
    if get is None:
        with httpx.Client(timeout=20.0, headers=HEADERS) as client:
            return _capture_all(data_dir, signals, roster, run_id, result,
                                http_get_json(client), now_fn, monotonic, max_seconds)
    return _capture_all(data_dir, signals, roster, run_id, result, get, now_fn, monotonic,
                        max_seconds)


def _capture_all(data_dir: Path, signals: list[dict[str, Any]], roster: dict[str, Any],
                 run_id: str, result: dict[str, Any], get: GetJson,
                 now_fn: Callable[[], datetime], monotonic: Callable[[], float],
                 max_seconds: float) -> dict[str, Any]:
    started = monotonic()
    cids = sorted({s["condition_id"] for s in signals})
    try:
        gamma = _gamma_fees(cids, get)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("cohort capture: Gamma fee lookup failed: %s", type(exc).__name__)
        gamma = {}
    markets: dict[str, dict[str, Any] | None] = {}
    books: dict[str, Any] = {}
    excluded: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for signal in signals:
        cid = signal["condition_id"]
        base = {"run_id": run_id, "signal_id": f"{run_id}:{signal['wallet']}:{cid}:"
                f"{signal['outcome_index']}", "cohort_id": roster.get("cohort_id"),
                "config_hash": roster.get("config_hash"), "membership_mode": roster.get("mode")}
        if monotonic() - started > max_seconds:
            row = {**base, **signal, "detected_at": now_fn().isoformat(),
                   "exclusion_reasons": ["capture time cap reached"]}
        else:
            if cid not in markets:
                try:
                    markets[cid] = _market(cid, get)
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("cohort capture: CLOB market lookup failed: %s",
                                type(exc).__name__)
                    markets[cid] = None
            row = {**base, **_capture_one(signal, markets[cid], gamma.get(cid), get, books,
                                          now_fn())}
        row["status"] = "excluded" if row["exclusion_reasons"] else "captured"
        excluded.update(row["exclusion_reasons"])
        rows.append(row)
    path = data_dir / STAGE_DIR / SIGNALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _trim_torn_tail(path)
    with path.open("a", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    result["captured"] = sum(1 for row in rows if row["status"] == "captured")
    result["excluded"] = dict(sorted(excluded.items()))
    result["seconds"] = round(monotonic() - started, 1)
    log.info("cohort capture: %d signals, %d captured, %d stale fills, exclusions %s",
             len(rows), result["captured"], result["stale_fills"], result["excluded"])
    return result
