"""
Exit signals — skilled wallets closing or materially trimming held positions.

The feed surfaces still-held BUY entries; when a skilled wallet sells down a
position, the row silently disappears. That disappearance IS the signal: a
tailing user holding the same market wants to know the wallet they followed
has un-wound. This service turns it into a first-class signal type.

Detection
---------
`polymarket_positions.jsonl` is append-only (one row per position per
snapshot) and `polymarket_position_snapshots.jsonl` records each wallet's
latest COMPLETE snapshot. We keep our own watermark (`exit_state.json`,
wallet → last-diffed snapshot_id). When the manifest advances past the
watermark, both snapshots are known-complete, so a position present in the
old snapshot and shrunk/gone in the new one is a real sell:

  closed   current size ~ 0
  trimmed  size reduced by >= 50%

False-positive guards:
  - First sighting of a wallet initializes the watermark without emitting
    (no history to diff against).
  - Exits are only emitted while Gamma still marks the market active: when a
    market resolves, positions leave the snapshot via redemption, which is
    settlement — not a sell. Skipping closed/missing markets removes that
    entire failure mode.
  - Dust positions (under $1 of value in the old snapshot) are ignored.

Each exit row records whether the position had previously been surfaced by
the feed (`was_surfaced`, via the signal ledger), so consumers can prioritize
"un-tail" alerts for signals they may have acted on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import (
    exit_signals_path,
    exit_state_path,
    polymarket_position_snapshots_path,
    polymarket_positions_path,
    signal_ledger_path,
)
from marketsignalos_api.services.external_urls import (
    polymarket_market_url,
    polymarket_profile_url,
)
from marketsignalos_api.services.skilled_bets import (
    _f,
    _i,
    _load_polymarket_markets,
    _load_skilled_wallets,
    _read_jsonl,
)

log = logging.getLogger("marketsignalos.api.exit_signals")

_DUST_SIZE = 1e-9
_MIN_PREVIOUS_VALUE_USDC = 1.0
_TRIM_THRESHOLD = 0.5


@dataclass(slots=True)
class ExitSignal:
    proxy_wallet: str
    wallet_name: str
    skill_likelihood: float

    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    event_slug: str

    exit_type: str  # "closed" | "trimmed"
    previous_size: float
    current_size: float
    reduction_pct: float  # 0..1 fraction of the position sold
    previous_value_usdc: float
    current_outcome_price: float

    from_snapshot_id: str
    to_snapshot_id: str
    from_snapshot_at: str
    to_snapshot_at: str

    was_surfaced: bool        # position previously recorded in the signal ledger
    surfaced_recorded_at: str

    polymarket_market_url: str
    polymarket_profile_url: str
    detected_at: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state(path: Path) -> dict[str, str]:
    """wallet -> last-diffed snapshot_id."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(wallet): str(snapshot_id)
        for wallet, snapshot_id in raw.items()
        if snapshot_id
    }


def _write_state_atomic(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _load_manifests() -> dict[str, dict[str, Any]]:
    """wallet -> latest complete snapshot manifest row."""
    out: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(polymarket_position_snapshots_path()):
        if row.get("complete") is not True:
            continue
        wallet = str(row.get("proxy_wallet", "")).lower()
        snapshot_id = str(row.get("snapshot_id", ""))
        if wallet and snapshot_id:
            out[wallet] = row
    return out


def _load_positions_by_snapshot(
    wallets: set[str],
) -> dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]]:
    """(wallet, snapshot_id) -> {(condition_id, outcome_index): position row}."""
    out: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]] = {}
    for row in _read_jsonl(polymarket_positions_path()):
        wallet = str(row.get("proxy_wallet", "")).lower()
        if wallet not in wallets:
            continue
        snapshot_id = str(row.get("snapshot_id", ""))
        cond = str(row.get("condition_id", ""))
        if not snapshot_id or not cond:
            continue
        key = (cond, _i(row.get("outcome_index")))
        out.setdefault((wallet, snapshot_id), {})[key] = row
    return out


def _load_surfaced_index() -> dict[tuple[str, str, int], str]:
    """(wallet, condition, outcome) -> recorded_at for ledger-surfaced signals."""
    out: dict[tuple[str, str, int], str] = {}
    for row in _read_jsonl(signal_ledger_path()):
        key = (
            str(row.get("proxy_wallet", "")).lower(),
            str(row.get("condition_id", "")),
            _i(row.get("outcome_index")),
        )
        recorded_at = str(row.get("recorded_at", ""))
        if recorded_at and key not in out:
            out[key] = recorded_at
    return out


def update_exit_signals() -> dict[str, int]:
    """
    One detection pass: diff each skilled wallet's latest complete snapshot
    against the last one we examined, append new exit rows, and advance the
    watermark. Called after every ingest run and via
    POST /signals/exits/refresh.
    """
    skilled = _load_skilled_wallets(min_skill=0.8, min_resolved=20)
    manifests = _load_manifests()
    state_path = exit_state_path()
    state = _read_state(state_path)

    # Wallets whose snapshot advanced — only these need a diff.
    pending: list[tuple[str, str, str]] = []  # (wallet, old_snapshot, new_snapshot)
    initialized = 0
    for wallet in skilled:
        manifest = manifests.get(wallet)
        if manifest is None:
            continue
        new_snapshot = str(manifest.get("snapshot_id", ""))
        old_snapshot = state.get(wallet, "")
        if not old_snapshot:
            state[wallet] = new_snapshot
            initialized += 1
            continue
        if old_snapshot != new_snapshot:
            pending.append((wallet, old_snapshot, new_snapshot))

    detected: list[ExitSignal] = []
    if pending:
        positions = _load_positions_by_snapshot({w for w, _, _ in pending})
        markets = _load_polymarket_markets()
        surfaced = _load_surfaced_index()
        now = _utcnow_iso()

        for wallet, old_snapshot, new_snapshot in pending:
            prev_positions = positions.get((wallet, old_snapshot))
            curr_positions = positions.get((wallet, new_snapshot), {})
            state[wallet] = new_snapshot
            if prev_positions is None:
                # Old snapshot rows no longer on disk — can't diff honestly.
                continue
            info = skilled[wallet]
            for key, old_row in prev_positions.items():
                prev_size = _f(old_row.get("size"))
                prev_value = _f(old_row.get("current_value_usdc"))
                if prev_size <= _DUST_SIZE or prev_value < _MIN_PREVIOUS_VALUE_USDC:
                    continue
                cond, outcome_idx = key
                market = markets.get(cond)
                if market is None or market.closed or not market.active:
                    # Resolved/closed markets shed positions via redemption —
                    # settlement, not a sell. Never alert on those.
                    continue
                new_row = curr_positions.get(key)
                cur_size = _f(new_row.get("size")) if new_row else 0.0
                if cur_size <= _DUST_SIZE:
                    exit_type = "closed"
                    cur_size = 0.0
                else:
                    reduction = (prev_size - cur_size) / prev_size
                    if reduction < _TRIM_THRESHOLD:
                        continue
                    exit_type = "trimmed"
                reduction_pct = (
                    1.0 if cur_size == 0.0 else (prev_size - cur_size) / prev_size
                )
                surfaced_at = surfaced.get((wallet, cond, outcome_idx), "")
                event_slug = str(old_row.get("event_slug", "") or "")
                current_price = (
                    _f(new_row.get("current_outcome_price"))
                    if new_row
                    else market.yes_price
                    if outcome_idx == 0
                    else (1.0 - market.yes_price if market.yes_price > 0 else 0.0)
                )
                detected.append(
                    ExitSignal(
                        proxy_wallet=wallet,
                        wallet_name=info.name,
                        skill_likelihood=round(info.skill_likelihood, 6),
                        condition_id=cond,
                        outcome_index=outcome_idx,
                        outcome=str(old_row.get("outcome", "")),
                        title=str(old_row.get("title", "")),
                        event_slug=event_slug,
                        exit_type=exit_type,
                        previous_size=round(prev_size, 4),
                        current_size=round(cur_size, 4),
                        reduction_pct=round(reduction_pct, 4),
                        previous_value_usdc=round(prev_value, 2),
                        current_outcome_price=round(current_price, 4),
                        from_snapshot_id=old_snapshot,
                        to_snapshot_id=new_snapshot,
                        from_snapshot_at=str(old_row.get("snapshot_at", "") or ""),
                        to_snapshot_at=(
                            str(new_row.get("snapshot_at", "") or "")
                            if new_row
                            else str(
                                manifests.get(wallet, {}).get("snapshot_at", "") or ""
                            )
                        ),
                        was_surfaced=bool(surfaced_at),
                        surfaced_recorded_at=surfaced_at,
                        polymarket_market_url=polymarket_market_url(event_slug),
                        polymarket_profile_url=polymarket_profile_url(wallet),
                        detected_at=now,
                    )
                )

    # Append new rows, deduped against what's already on disk. The watermark
    # makes duplicates structurally unlikely; the key check covers replays
    # after a lost state file.
    path = exit_signals_path()
    existing = _read_jsonl(path)
    existing_keys = {
        (
            str(r.get("proxy_wallet", "")),
            str(r.get("condition_id", "")),
            _i(r.get("outcome_index")),
            str(r.get("to_snapshot_id", "")),
        )
        for r in existing
    }
    new_rows = [
        e
        for e in detected
        if (e.proxy_wallet, e.condition_id, e.outcome_index, e.to_snapshot_id)
        not in existing_keys
    ]
    if new_rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for signal in new_rows:
                fh.write(json.dumps(asdict(signal), separators=(",", ":")) + "\n")
    _write_state_atomic(state_path, state)

    counts = {
        "detected_closed": sum(1 for e in new_rows if e.exit_type == "closed"),
        "detected_trimmed": sum(1 for e in new_rows if e.exit_type == "trimmed"),
        "initialized_wallets": initialized,
        "diffed_wallets": len(pending),
        "total_rows": len(existing) + len(new_rows),
    }
    log.info("exit signals updated %s", counts)
    return counts


def list_exit_signals(
    *, exit_type: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Detected exits, newest first."""
    rows = _read_jsonl(exit_signals_path())
    if exit_type:
        rows = [r for r in rows if str(r.get("exit_type", "")) == exit_type]
    rows.sort(
        key=lambda r: (str(r.get("detected_at", "")), str(r.get("to_snapshot_at", ""))),
        reverse=True,
    )
    return rows[:limit]
