"""Collect a small, public, descriptive sample; never writes the scoring stores."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://data-api.polymarket.com"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "artifacts"
    / "marketsignalos-dashboard/public/data/research-snapshot.json"
)
MAX_RESPONSE_BYTES = 1_000_000
MAX_SNAPSHOT_BYTES = 2_000_000
TRADE_LIMIT = 100
POSITION_LIMIT = 50
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
LOG = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def number(value: Any, maximum: float = 1e15) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= maximum else None


def label(value: Any, fallback: str = "Unknown") -> str:
    return value[:300] if isinstance(value, str) and value.strip() else fallback


def market_fields(row: dict[str, Any], address: str) -> dict[str, Any]:
    if str(row.get("proxyWallet", "")).lower() != address:
        raise ValueError("wallet mismatch")
    condition = row.get("conditionId", "")
    asset = row.get("asset", "")
    if not isinstance(condition, str) or not HASH.fullmatch(condition):
        raise ValueError("invalid condition")
    if not isinstance(asset, str) or not re.fullmatch(r"[0-9]{1,100}", asset):
        raise ValueError("invalid asset")
    slug = row.get("eventSlug", "")
    return {
        "condition_id": condition.lower(),
        "asset": asset,
        "title": label(row.get("title")),
        "outcome": label(row.get("outcome")),
        "event_slug": slug
        if isinstance(slug, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,250}", slug)
        else None,
    }


class Collector:
    def __init__(
        self, client: httpx.Client, wallet_limit: int = 10, max_seconds: int = 180
    ):
        if not 1 <= wallet_limit <= 20 or not 10 <= max_seconds <= 300:
            raise ValueError("Use 1–20 wallets and 10–300 seconds")
        self.client = client
        self.wallet_limit = wallet_limit
        self.max_seconds = max_seconds
        self.started_at = utc_now()
        self.end = int(time.time())
        self.start = self.end - 7 * 86400
        self.deadline = time.monotonic() + max_seconds
        self.requests: list[dict[str, Any]] = []

    def fetch(
        self, path: str, params: dict[str, Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        receipt: dict[str, Any] = {
            "path": path,
            "params": params,
            "requested_at": utc_now(),
            "status": "unavailable",
            "rows_returned": 0,
        }
        self.requests.append(receipt)
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("collection deadline reached; request skipped")
            # No retries, redirects, pagination, credentials, or unbounded response reads.
            with self.client.stream(
                "GET", BASE_URL + path, params=params, timeout=min(8, remaining)
            ) as response:
                receipt["http_status"] = response.status_code
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    if time.monotonic() > self.deadline:
                        raise ValueError("collection deadline reached")
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("response size limit exceeded")
                receipt["sha256"] = hashlib.sha256(body).hexdigest()
                rows = json.loads(body)
                if not isinstance(rows, list) or len(rows) > params["limit"]:
                    raise ValueError("unexpected response shape or row limit exceeded")
                receipt.update(status="ok", rows_returned=len(rows))
                return rows, receipt
        except (httpx.HTTPError, ValueError) as exc:
            # Do not include response bodies, headers, or credentials in published errors.
            receipt["error"] = type(exc).__name__
            LOG.warning("Collection unavailable for %s: %s", path, exc)
            return [], receipt
        finally:
            receipt["observed_at"] = utc_now()

    def sample(self, address: str, kind: str) -> dict[str, Any]:
        limit = TRADE_LIMIT if kind == "trades" else POSITION_LIMIT
        params: dict[str, Any] = {
            "user": address,
            "limit": limit,
            "offset": 0,
            "sortDirection": "DESC",
        }
        if kind == "trades":
            params.update(
                type="TRADE", sortBy="TIMESTAMP", start=self.start, end=self.end
            )
            path = "/activity"
        else:
            params.update(sortBy="CURRENT", sizeThreshold=0, redeemable="false")
            path = "/positions"
        raw, receipt = self.fetch(path, params)
        rows = []
        rejected = 0
        seen_assets: set[str] = set()
        for row in raw:
            try:
                if not isinstance(row, dict):
                    raise ValueError("invalid row")
                normalized = market_fields(row, address)
                size = number(row.get("size"))
                if size is None or size <= 0:
                    raise ValueError("invalid size")
                normalized["size"] = size
                if kind == "trades":
                    stamp = row.get("timestamp")
                    price = number(row.get("price"), 1)
                    if (
                        row.get("type") != "TRADE"
                        or row.get("side") not in {"BUY", "SELL"}
                        or type(stamp) is not int
                        or not self.start <= stamp <= self.end
                        or price is None
                    ):
                        raise ValueError("invalid trade")
                    tx = row.get("transactionHash", "")
                    normalized.update(
                        timestamp=stamp,
                        side=row["side"],
                        price=price,
                        notional=number(row.get("usdcSize")),
                        transaction_hash=tx
                        if isinstance(tx, str) and HASH.fullmatch(tx)
                        else None,
                    )
                else:
                    if (
                        normalized["asset"] in seen_assets
                        or row.get("redeemable") is not False
                    ):
                        raise ValueError("duplicate asset or redeemability unavailable")
                    seen_assets.add(normalized["asset"])
                    normalized.update(
                        average_price=number(row.get("avgPrice"), 1),
                        reported_price=number(row.get("curPrice"), 1),
                        reported_value=number(row.get("currentValue")),
                    )
                rows.append(normalized)
            except ValueError:
                rejected += 1
        if kind == "trades":
            rows.sort(key=lambda item: item["timestamp"], reverse=True)
        receipt["rows_rejected"] = rejected
        return {
            "status": receipt["status"],
            "observed_at": receipt["observed_at"],
            "rows_returned": len(raw),
            "rows_rejected": rejected,
            "possibly_truncated": len(raw) >= limit,
            "rows": rows,
        }

    def collect(self) -> dict[str, Any]:
        seeds, receipt = self.fetch(
            "/v1/leaderboard",
            {
                "category": "OVERALL",
                "timePeriod": "DAY",
                "orderBy": "VOL",
                "limit": self.wallet_limit,
                "offset": 0,
            },
        )
        wallets = []
        seen: set[str] = set()
        rejected = 0
        for seed in seeds:
            address = (
                str(seed.get("proxyWallet", "")).lower()
                if isinstance(seed, dict)
                else ""
            )
            if not ADDRESS.fullmatch(address) or address in seen:
                rejected += 1
                continue
            seen.add(address)
            wallets.append(
                {
                    "address": address,
                    "name": label(seed.get("userName"), address),
                    "evaluation_status": "not_evaluated",
                    "trades": self.sample(address, "trades"),
                    "positions": self.sample(address, "positions"),
                }
            )
        receipt["rows_rejected"] = rejected
        if not wallets or not any(
            w[k]["rows"] for w in wallets for k in ("trades", "positions")
        ):
            raise ValueError("No usable observations; previous snapshot preserved")
        return {
            "schema_version": 1,
            "collector_version": "research-snapshot-v1",
            "started_at": self.started_at,
            "generated_at": utc_now(),
            "source": BASE_URL,
            "selection": "OVERALL / DAY / VOL, first page",
            "status": "partial"
            if any(r["status"] != "ok" or r.get("rows_rejected") for r in self.requests)
            else "complete",
            "limits": {
                "wallets": self.wallet_limit,
                "trades_per_wallet": TRADE_LIMIT,
                "positions_per_wallet": POSITION_LIMIT,
                "max_requests": 1 + 2 * self.wallet_limit,
                "max_seconds": self.max_seconds,
                "max_response_bytes": MAX_RESPONSE_BYTES,
            },
            "trade_window": {"start": self.start, "end": self.end},
            "wallets": wallets,
            "requests": self.requests,
        }


def publish(snapshot: dict[str, Any], output: Path) -> None:
    content = (
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot too large; previous snapshot preserved")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Explicitly collect from the public API"
    )
    parser.add_argument("--wallet-limit", type=int, default=10)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.run:
        parser.error("Pass --run to collect; no network request was made")
    with httpx.Client(
        follow_redirects=False,
        headers={"User-Agent": "MarketSignalOS/research-snapshot-v1"},
    ) as client:
        snapshot = Collector(client, args.wallet_limit, args.max_seconds).collect()
    publish(snapshot, args.output)
    LOG.info(
        "Published %d wallets (%s) to %s",
        len(snapshot["wallets"]),
        snapshot["status"],
        args.output,
    )


if __name__ == "__main__":
    main()
