"""
Push distribution for new signals: one webhook POST per ingest pass.

Freshness is a product feature, but a pull-only dashboard means a 2-hour-old
entry by a top wallet can go unseen for days. This notifier watches the two
persistent signal streams (the paper-trade ledger and exit signals) and
delivers anything new to a configured webhook.

Configuration: set `SIGNAL_WEBHOOK_URL` to any HTTP(S) endpoint that accepts
a JSON POST (Slack/Discord-compatible relays, Zapier, a personal bot, ...).
Unset → the pass is a silent no-op apart from watermark bookkeeping.

Semantics
---------
- Watermarks (`notifications_state.json`) track the newest `recorded_at` /
  `detected_at` already handled per stream.
- The first-ever pass initializes watermarks to the current stream heads and
  sends nothing — configuring a webhook must not flood it with the entire
  historical backlog.
- Watermarks advance on successful delivery (or when there is nothing to
  send, or no webhook is configured). A failed POST leaves them untouched so
  the next pass retries the same batch. Delivery is therefore at-least-once;
  receivers should treat (proxy_wallet, condition_id, outcome_index) as the
  idempotency key.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from marketsignalos_api._paths import (
    exit_signals_path,
    notifications_state_path,
    signal_ledger_path,
)
from marketsignalos_api.services.external_urls import polymarket_market_url
from marketsignalos_api.services.skilled_bets import _read_jsonl

log = logging.getLogger("marketsignalos.api.notifications")

_WEBHOOK_ENV = "SIGNAL_WEBHOOK_URL"
_TIMEOUT_SECONDS = 5.0
_MAX_ITEMS_PER_STREAM = 25


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _signal_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "skilled_bet",
        "wallet_name": str(row.get("wallet_name", "")),
        "proxy_wallet": str(row.get("proxy_wallet", "")),
        "title": str(row.get("title", "")),
        "outcome": str(row.get("outcome", "")),
        "entry_price": row.get("entry_price", 0.0),
        "surface_price": row.get("surface_price", 0.0),
        "tradability": str(row.get("tradability", "")),
        "skill_likelihood": row.get("skill_likelihood", 0.0),
        "polymarket_market_url": polymarket_market_url(
            str(row.get("event_slug", "") or "")
        ),
        "recorded_at": str(row.get("recorded_at", "")),
    }


def _exit_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "exit",
        "wallet_name": str(row.get("wallet_name", "")),
        "proxy_wallet": str(row.get("proxy_wallet", "")),
        "title": str(row.get("title", "")),
        "outcome": str(row.get("outcome", "")),
        "exit_type": str(row.get("exit_type", "")),
        "reduction_pct": row.get("reduction_pct", 0.0),
        "previous_value_usdc": row.get("previous_value_usdc", 0.0),
        "was_surfaced": bool(row.get("was_surfaced", False)),
        "polymarket_market_url": str(row.get("polymarket_market_url", "")),
        "detected_at": str(row.get("detected_at", "")),
    }


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """Module-level so tests can monkeypatch the transport."""
    response = httpx.post(url, json=payload, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()


def run_notification_pass() -> dict[str, Any]:
    """
    Collect ledger/exit rows newer than the stored watermarks and deliver
    them in one webhook POST. Called after every ingest run and via
    POST /signals/notifications/run.
    """
    webhook_url = os.getenv(_WEBHOOK_ENV, "").strip()
    state_path = notifications_state_path()
    state = _read_state(state_path)

    ledger_rows = _read_jsonl(signal_ledger_path())
    exit_rows = _read_jsonl(exit_signals_path())
    ledger_head = max(
        (str(r.get("recorded_at", "")) for r in ledger_rows), default=""
    )
    exit_head = max((str(r.get("detected_at", "")) for r in exit_rows), default="")

    if not state.get("initialized"):
        # First pass ever: mark current stream heads as seen, deliver nothing.
        _write_state_atomic(
            state_path,
            {
                "initialized": True,
                "ledger_watermark": ledger_head,
                "exit_watermark": exit_head,
                "last_sent_at": "",
                "last_error": "",
            },
        )
        log.info("notifications initialized (no backlog delivery)")
        return {
            "initialized": True,
            "sent": False,
            "new_signals": 0,
            "new_exits": 0,
            "webhook_configured": bool(webhook_url),
        }

    ledger_watermark = str(state.get("ledger_watermark", ""))
    exit_watermark = str(state.get("exit_watermark", ""))
    new_signals = sorted(
        (
            r
            for r in ledger_rows
            if str(r.get("recorded_at", "")) > ledger_watermark
        ),
        key=lambda r: str(r.get("recorded_at", "")),
    )
    new_exits = sorted(
        (r for r in exit_rows if str(r.get("detected_at", "")) > exit_watermark),
        key=lambda r: str(r.get("detected_at", "")),
    )

    result: dict[str, Any] = {
        "initialized": False,
        "sent": False,
        "new_signals": len(new_signals),
        "new_exits": len(new_exits),
        "webhook_configured": bool(webhook_url),
    }

    if not new_signals and not new_exits:
        return result

    if webhook_url:
        payload = {
            "source": "marketsignalos",
            "generated_at": _utcnow_iso(),
            "new_signals": [
                _signal_summary(r) for r in new_signals[:_MAX_ITEMS_PER_STREAM]
            ],
            "new_exits": [
                _exit_summary(r) for r in new_exits[:_MAX_ITEMS_PER_STREAM]
            ],
            "truncated": (
                len(new_signals) > _MAX_ITEMS_PER_STREAM
                or len(new_exits) > _MAX_ITEMS_PER_STREAM
            ),
        }
        try:
            _post_webhook(webhook_url, payload)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            # Watermarks stay put: the next pass retries this same batch.
            state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            _write_state_atomic(state_path, state)
            log.warning("webhook delivery failed: %s", state["last_error"])
            result["error"] = state["last_error"]
            return result
        result["sent"] = True
        state["last_sent_at"] = _utcnow_iso()

    # Mark the batch handled: delivered, or intentionally dropped because no
    # webhook is configured (prevents a later-configured webhook from being
    # flooded with stale items).
    state["initialized"] = True
    state["ledger_watermark"] = max(ledger_watermark, ledger_head)
    state["exit_watermark"] = max(exit_watermark, exit_head)
    state["last_error"] = ""
    _write_state_atomic(state_path, state)
    log.info(
        "notification pass complete sent=%s signals=%d exits=%d",
        result["sent"],
        len(new_signals),
        len(new_exits),
    )
    return result


def notification_status() -> dict[str, Any]:
    """Operational snapshot: configuration plus the persisted watermarks."""
    state = _read_state(notifications_state_path())
    return {
        "webhook_configured": bool(os.getenv(_WEBHOOK_ENV, "").strip()),
        "initialized": bool(state.get("initialized", False)),
        "ledger_watermark": str(state.get("ledger_watermark", "")),
        "exit_watermark": str(state.get("exit_watermark", "")),
        "last_sent_at": str(state.get("last_sent_at", "")),
        "last_error": str(state.get("last_error", "")),
    }
