"""
Fast-lane watchlist poller: low-latency BUY / SELL alerts for top wallets.

The scheduled pipeline bounds webhook latency to the ingest interval —
typically an hour. Informed flow gets priced in much faster than that, so a
tail alert that arrives an hour late has usually forfeited the edge. This
poller closes the gap for the wallets that matter: every FASTLANE_EVERY_SECONDS
it polls ONLY the activity endpoint for the top-N tailable wallets and
webhook-delivers any new TRADE immediately.

Design constraints:

- **Alert-only.** The fast lane never writes to the JSONL stores. The
  pipeline remains the sole writer (its activity fetch dedupes via the
  store's sidecar index and will pick these same events up on its next
  pass), so the poller cannot race the pipeline or corrupt the dedupe
  index. The webhook payload carries everything needed to act on the alert.
- **No backlog floods.** The first time a wallet is polled, its watermark is
  initialized to the newest event seen and nothing is delivered — the same
  semantics as the batch notifier.
- **At-least-once.** Watermarks only advance after a successful webhook POST
  (or when there is nothing to send / no webhook configured). A failed POST
  is retried with the same batch on the next tick; receivers should treat
  (proxy_wallet, transaction_hash, condition_id, outcome_index) as the
  idempotency key.

Configuration (all optional; the poller is off by default):

  FASTLANE_EVERY_SECONDS   > 0 enables the loop (e.g. 90). Values below 30
                           are clamped to 30 to stay polite to the data API.
  FASTLANE_WALLETS         wallets polled per tick, ranked by rank_score
                           (default 25, capped at 100)
  FASTLANE_MIN_ENTRY_USDC  ignore trades below this size (default 0 = all)
  SIGNAL_WEBHOOK_URL       shared with the batch notifier; unset → the tick
                           still tracks watermarks but delivers nothing
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import fastlane_state_path, polymarket_enrichment_path
from marketsignalos_api.services.external_urls import polymarket_market_url
from marketsignalos_api.services.notifications import _post_webhook
from marketsignalos_api.services.skilled_bets import _f, _read_jsonl

log = logging.getLogger("marketsignalos.api.fastlane")

_WEBHOOK_ENV = "SIGNAL_WEBHOOK_URL"
_STARTUP_DELAY_SECONDS = 30.0
_MIN_INTERVAL_SECONDS = 30
_MAX_WALLETS = 100
_MAX_ALERTS_PER_STREAM = 25
_ACTIVITY_PAGE_LIMIT = 500

# fetch(address, since_ts_or_None) -> raw activity rows (data-API camelCase).
FetchFn = Callable[[str, int | None], list[dict[str, Any]]]
PostFn = Callable[[str, dict[str, Any]], None]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-integer %s=%r", name, raw)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r", name, raw)
        return default


@dataclass(frozen=True, slots=True)
class FastlaneConfig:
    interval_seconds: int
    max_wallets: int
    min_entry_usdc: float

    @classmethod
    def from_env(cls) -> FastlaneConfig | None:
        """None (poller off) unless FASTLANE_EVERY_SECONDS is a positive int."""
        interval = _env_int("FASTLANE_EVERY_SECONDS", 0)
        if interval <= 0:
            return None
        return cls(
            interval_seconds=max(_MIN_INTERVAL_SECONDS, interval),
            max_wallets=min(_MAX_WALLETS, max(1, _env_int("FASTLANE_WALLETS", 25))),
            min_entry_usdc=max(0.0, _env_float("FASTLANE_MIN_ENTRY_USDC", 0.0)),
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Watchlist selection ──────────────────────────────────────────────────────

@dataclass(slots=True)
class _WatchedWallet:
    proxy_wallet: str
    name: str
    skill_likelihood: float
    wallet_archetype: str


def _load_watchlist(max_wallets: int) -> list[_WatchedWallet]:
    """Top tailable wallets by conservative rank score. Same trust gates the
    feed applies: trusted data + tailable status."""
    ranked: list[tuple[float, float, _WatchedWallet]] = []
    for row in _read_jsonl(polymarket_enrichment_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if not wallet:
            continue
        if (
            str(row.get("data_quality_status", "untrusted")) != "trusted"
            or str(row.get("tailability_status", "blocked")) != "tailable"
        ):
            continue
        ranked.append(
            (
                _f(row.get("rank_score")),
                _f(row.get("skill_likelihood")),
                _WatchedWallet(
                    proxy_wallet=wallet,
                    name=str(row.get("name", "") or row.get("pseudonym", "") or ""),
                    skill_likelihood=_f(row.get("skill_likelihood")),
                    wallet_archetype=str(
                        row.get("style_archetype", "") or "unclassified"
                    ),
                ),
            )
        )
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].proxy_wallet))
    return [item[2] for item in ranked[:max_wallets]]


# ── State ────────────────────────────────────────────────────────────────────

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


# ── Fetch + alert assembly ───────────────────────────────────────────────────

def _default_fetch(address: str, since_ts: int | None) -> list[dict[str, Any]]:
    """One activity page per wallet per tick. A wallet doing >500 trades
    inside one interval overflows the page and the excess is only alerted by
    the next full pipeline pass — acceptable for an alerting sidecar."""
    from marketsignalos_polymarket.polymarket_client import (  # noqa: PLC0415
        PolymarketClient,
    )

    client = PolymarketClient()
    try:
        rows: list[dict[str, Any]] = client.get_wallet_activity(
            address, limit=_ACTIVITY_PAGE_LIMIT, start=since_ts
        )
        return rows
    finally:
        client.close()


def _alert_from_event(wallet: _WatchedWallet, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "fastlane_trade",
        "side": str(event.get("side", "") or "").upper(),
        "proxy_wallet": wallet.proxy_wallet,
        "wallet_name": wallet.name,
        "skill_likelihood": wallet.skill_likelihood,
        "wallet_archetype": wallet.wallet_archetype,
        "title": str(event.get("title", "") or ""),
        "outcome": str(event.get("outcome", "") or ""),
        "outcome_index": int(event.get("outcomeIndex", 0) or 0),
        "price": _f(event.get("price")),
        "usdc_size": _f(event.get("usdcSize")),
        "condition_id": str(event.get("conditionId", "") or ""),
        "transaction_hash": str(event.get("transactionHash", "") or ""),
        "timestamp": int(event.get("timestamp", 0) or 0),
        "polymarket_market_url": polymarket_market_url(
            str(event.get("eventSlug", "") or "")
        ),
    }


def run_fastlane_tick(
    *,
    fetch: FetchFn | None = None,
    post: PostFn | None = None,
) -> dict[str, Any]:
    """
    One polling pass over the fast-lane watchlist. Called by the background
    loop and by POST /signals/fastlane/run.

    Returns a summary dict (also stored as `last_result` in the status).
    """
    fetch = fetch or _default_fetch
    post = post or _post_webhook
    max_wallets = min(_MAX_WALLETS, max(1, _env_int("FASTLANE_WALLETS", 25)))
    min_usdc = max(0.0, _env_float("FASTLANE_MIN_ENTRY_USDC", 0.0))
    webhook_url = os.getenv(_WEBHOOK_ENV, "").strip()

    state_path = fastlane_state_path()
    state = _read_state(state_path)
    watermarks_raw = state.get("wallets")
    watermarks: dict[str, int] = (
        {str(k): int(v) for k, v in watermarks_raw.items()}
        if isinstance(watermarks_raw, dict)
        else {}
    )

    watchlist = _load_watchlist(max_wallets)
    new_buys: list[dict[str, Any]] = []
    new_sells: list[dict[str, Any]] = []
    # Watermark advances are staged and only committed after delivery
    # succeeds (at-least-once semantics; see module docstring).
    staged_watermarks: dict[str, int] = {}
    initialized_wallets = 0
    fetch_errors = 0

    for wallet in watchlist:
        since = watermarks.get(wallet.proxy_wallet)
        try:
            rows = fetch(wallet.proxy_wallet, since)
        except Exception as exc:  # noqa: BLE001 — one flaky wallet must not kill the tick
            fetch_errors += 1
            log.warning(
                "fastlane fetch failed wallet=%s: %s", wallet.proxy_wallet, exc
            )
            continue
        newest = max(
            (int(r.get("timestamp", 0) or 0) for r in rows), default=since or 0
        )
        if since is None:
            # First sight of this wallet: mark current head seen, alert nothing.
            staged_watermarks[wallet.proxy_wallet] = newest
            initialized_wallets += 1
            continue
        staged_watermarks[wallet.proxy_wallet] = max(since, newest)
        for event in rows:
            if str(event.get("type", "")) != "TRADE":
                continue
            if int(event.get("timestamp", 0) or 0) <= since:
                continue
            if _f(event.get("usdcSize")) < min_usdc:
                continue
            side = str(event.get("side", "") or "").upper()
            if side == "BUY":
                new_buys.append(_alert_from_event(wallet, event))
            elif side == "SELL":
                new_sells.append(_alert_from_event(wallet, event))

    new_buys.sort(key=lambda a: -a["timestamp"])
    new_sells.sort(key=lambda a: -a["timestamp"])

    result: dict[str, Any] = {
        "ticked_at": _utcnow_iso(),
        "wallets_watched": len(watchlist),
        "wallets_initialized": initialized_wallets,
        "fetch_errors": fetch_errors,
        "new_buys": len(new_buys),
        "new_sells": len(new_sells),
        "sent": False,
        "webhook_configured": bool(webhook_url),
    }

    if webhook_url and (new_buys or new_sells):
        payload = {
            "source": "marketsignalos",
            "stream": "fastlane",
            "generated_at": _utcnow_iso(),
            "new_buys": new_buys[:_MAX_ALERTS_PER_STREAM],
            "new_sells": new_sells[:_MAX_ALERTS_PER_STREAM],
            "truncated": (
                len(new_buys) > _MAX_ALERTS_PER_STREAM
                or len(new_sells) > _MAX_ALERTS_PER_STREAM
            ),
        }
        try:
            post(webhook_url, payload)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            error = f"{type(exc).__name__}: {exc}"[:300]
            # Initialized wallets still commit (they alerted nothing);
            # everything else keeps its old watermark so the batch retries.
            for wallet_addr in list(staged_watermarks):
                if wallet_addr in watermarks:
                    staged_watermarks.pop(wallet_addr)
            watermarks.update(staged_watermarks)
            state.update(
                wallets=watermarks, last_tick_at=result["ticked_at"], last_error=error
            )
            _write_state_atomic(state_path, state)
            log.warning("fastlane webhook delivery failed: %s", error)
            result["error"] = error
            return result
        result["sent"] = True
        state["last_sent_at"] = result["ticked_at"]

    watermarks.update(staged_watermarks)
    state.update(
        wallets=watermarks, last_tick_at=result["ticked_at"], last_error=""
    )
    _write_state_atomic(state_path, state)
    if new_buys or new_sells or initialized_wallets:
        log.info(
            "fastlane tick wallets=%d buys=%d sells=%d sent=%s init=%d",
            len(watchlist),
            len(new_buys),
            len(new_sells),
            result["sent"],
            initialized_wallets,
        )
    return result


# ── Background loop ──────────────────────────────────────────────────────────

class FastlanePoller:
    """Runs run_fastlane_tick on a fixed interval inside the API process.

    Same shape as IngestScheduler: a single asyncio task started from the
    FastAPI lifespan. The tick itself is synchronous HTTP fan-out, so it runs
    on the default executor; an overlapping tick (previous one still in
    flight) is skipped and counted.
    """

    def __init__(
        self,
        config: FastlaneConfig,
        *,
        startup_delay_seconds: float = _STARTUP_DELAY_SECONDS,
    ) -> None:
        self._config = config
        self._startup_delay_seconds = startup_delay_seconds
        self._task: asyncio.Task[None] | None = None
        self._tick_running = False
        self.ticks_completed = 0
        self.ticks_skipped_busy = 0
        self.last_result: dict[str, Any] | None = None

    @property
    def config(self) -> FastlaneConfig:
        return self._config

    async def _run_tick(self) -> None:
        if self._tick_running:
            self.ticks_skipped_busy += 1
            log.info("fastlane tick skipped: previous tick still in flight")
            return
        self._tick_running = True
        try:
            loop = asyncio.get_running_loop()
            self.last_result = await loop.run_in_executor(None, run_fastlane_tick)
            self.ticks_completed += 1
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            log.exception("fastlane tick raised")
        finally:
            self._tick_running = False

    async def _loop(self) -> None:
        delay = min(self._startup_delay_seconds, float(self._config.interval_seconds))
        while True:
            await asyncio.sleep(delay)
            await self._run_tick()
            delay = float(self._config.interval_seconds)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval_seconds": self._config.interval_seconds,
            "max_wallets": self._config.max_wallets,
            "min_entry_usdc": self._config.min_entry_usdc,
            "ticks_completed": self.ticks_completed,
            "ticks_skipped_busy": self.ticks_skipped_busy,
            "last_result": self.last_result,
        }


_active: FastlanePoller | None = None


def start_fastlane_from_env() -> FastlanePoller | None:
    """Start the env-configured poller (called from the API lifespan).
    Returns None — and says why — when FASTLANE_EVERY_SECONDS is unset."""
    global _active
    config = FastlaneConfig.from_env()
    if config is None:
        log.info("fastlane poller disabled (FASTLANE_EVERY_SECONDS not set)")
        _active = None
        return None
    poller = FastlanePoller(config)
    poller.start()
    _active = poller
    log.info(
        "fastlane poller started interval_seconds=%d max_wallets=%d",
        config.interval_seconds,
        config.max_wallets,
    )
    return poller


async def stop_fastlane() -> None:
    global _active
    if _active is not None:
        await _active.stop()
        _active = None


def fastlane_status() -> dict[str, Any]:
    """Operational snapshot: poller state (if running) + persisted watermarks."""
    state = _read_state(fastlane_state_path())
    wallets = state.get("wallets")
    poller = _active.status() if _active is not None else {"enabled": False}
    return {
        "poller": poller,
        "webhook_configured": bool(os.getenv(_WEBHOOK_ENV, "").strip()),
        "wallets_tracked": len(wallets) if isinstance(wallets, dict) else 0,
        "last_tick_at": str(state.get("last_tick_at", "")),
        "last_sent_at": str(state.get("last_sent_at", "")),
        "last_error": str(state.get("last_error", "")),
    }
