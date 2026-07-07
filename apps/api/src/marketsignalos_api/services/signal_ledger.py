"""
Signal outcome ledger — the paper-trade record that makes the feed falsifiable.

Every time the skilled-bets feed surfaces a signal (under its default
filters), one immutable row is recorded with the prices a tailing user could
actually have gotten at that moment. When the underlying market resolves, the
row is settled and hypothetical tail ROI is computed two ways:

  roi_at_surface  $1 staked at the live outcome price when the signal was
                  first surfaced (what tailing the FEED would have returned)
  roi_at_entry    $1 staked at the wallet's own entry price (what tailing
                  the WALLET perfectly would have returned)

ROI per row: won → (1 - p) / p, lost → -1.0, void → 0.0.

Statuses:
  open   market not yet resolved
  won    picked outcome resolved YES
  lost   picked outcome resolved NO
  void   market closed without a single >=0.99 outcome (canceled/ambiguous)

The ledger is append-mostly JSONL: recording appends, settlement rewrites the
file atomically (it stays small — one row per surfaced signal, ever). A
(wallet, condition_id, outcome_index) key is recorded at most once; replays
of the same still-held position do not produce duplicate rows.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marketsignalos_api._paths import polymarket_markets_path, signal_ledger_path
from marketsignalos_api.services.skilled_bets import compute_skilled_bets

log = logging.getLogger("marketsignalos.api.signal_ledger")


@dataclass(slots=True)
class SignalLedgerRow:
    # Identity — one row per surfaced (wallet, market, outcome), ever.
    proxy_wallet: str
    condition_id: str
    outcome_index: int

    # Context frozen at record time.
    wallet_name: str
    outcome: str
    title: str
    event_slug: str
    tradability: str
    skill_likelihood: float
    edge_lower_bound: float
    entry_price: float          # wallet's own entry (per latest BUY)
    surface_price: float        # live picked-outcome price when surfaced; 0 if unknown
    surface_kalshi_price: float  # Kalshi mirror YES price when surfaced; 0 if none
    bought_at: int              # unix seconds of the wallet's latest BUY
    recorded_at: str            # ISO timestamp the ledger recorded the signal

    # Feed context frozen at record time so the summary can slice performance
    # by signal quality dimensions. Added after the first ledger rows shipped;
    # older rows lack these keys and readers bucket them as "unknown".
    category: str = ""
    remaining_edge_status: str = "unknown"
    move_captured_pct: float = 0.0
    conviction: str = "unknown"
    consensus_wallets: int = 1
    wallet_archetype: str = "unclassified"
    tail_fair_price: float = 0.0
    tail_ev: float = 0.0
    tail_ev_status: str = "unknown"
    tail_ev_source: str = "mark"
    spread_cents: float = 0.0
    book_status: str = "unknown"

    # Settlement.
    status: str = "open"        # open | won | lost | void
    settled_at: str = ""
    roi_at_surface: float | None = None
    roi_at_entry: float | None = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _write_rows_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    tmp.replace(path)


def _row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("proxy_wallet", "")).lower(),
        str(row.get("condition_id", "")),
        int(row.get("outcome_index", 0) or 0),
    )


def _load_latest_markets() -> dict[str, dict[str, Any]]:
    """condition_id -> the newest Gamma market row we have on file."""
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_rows(polymarket_markets_path()):
        cond = str(row.get("condition_id", ""))
        if not cond:
            continue
        existing = latest.get(cond)
        if existing is None or str(row.get("fetched_at", "")) > str(
            existing.get("fetched_at", "")
        ):
            latest[cond] = row
    return latest


def _resolved_outcomes(latest: dict[str, dict[str, Any]]) -> dict[str, int | None]:
    """
    condition_id -> winning outcome_index for resolved markets, or None for
    markets that closed without a single >=0.99 winner (canceled/ambiguous).
    Unresolved (still active / not closed) markets are absent entirely.

    Mirrors the strict settlement rule in the ingestor's skill computation:
    live markets trade at 99c all the time, so price alone is never evidence
    of resolution — the market must also be closed and inactive in Gamma.
    """
    out: dict[str, int | None] = {}
    for cond, row in latest.items():
        if not row.get("closed") or row.get("active"):
            continue
        prices = row.get("outcome_prices") or []
        if not isinstance(prices, list):
            continue
        winners = [
            i
            for i, price in enumerate(prices)
            if isinstance(price, (int, float)) and float(price) >= 0.99
        ]
        out[cond] = winners[0] if len(winners) == 1 else None
    return out


def _live_outcome_price(row: dict[str, Any] | None, outcome_index: int) -> float:
    """Latest observed price of one outcome, 0.0 when unavailable."""
    if row is None:
        return 0.0
    prices = row.get("outcome_prices") or []
    if not isinstance(prices, list) or not 0 <= outcome_index < len(prices):
        return 0.0
    price = prices[outcome_index]
    if not isinstance(price, (int, float)):
        return 0.0
    return float(price)


def _tail_roi(*, won: bool, price: float) -> float | None:
    """ROI of a $1 stake on the picked outcome at `price`. None if the price
    was unknown at record time (can't honestly score that variant)."""
    if price <= 0.0 or price >= 1.0:
        return None
    if won:
        return round((1.0 - price) / price, 4)
    return -1.0


def update_signal_ledger() -> dict[str, int]:
    """
    One ledger pass: record newly surfaced signals, then settle any open rows
    whose market has since resolved. Called after every ingest run and on
    demand via POST /signals/ledger/refresh.
    """
    path = signal_ledger_path()
    rows = _read_rows(path)
    known_keys = {_row_key(row) for row in rows}

    # 1. Record — same default filters the dashboard feed uses, uncapped.
    recorded = 0
    for signal in compute_skilled_bets(limit=None):
        key = (signal.proxy_wallet, signal.condition_id, signal.outcome_index)
        if key in known_keys:
            continue
        known_keys.add(key)
        recorded += 1
        rows.append(
            asdict(
                SignalLedgerRow(
                    proxy_wallet=signal.proxy_wallet,
                    condition_id=signal.condition_id,
                    outcome_index=signal.outcome_index,
                    wallet_name=signal.wallet_name,
                    outcome=signal.outcome,
                    title=signal.title,
                    event_slug=signal.event_slug,
                    tradability=signal.tradability,
                    skill_likelihood=signal.skill_likelihood,
                    edge_lower_bound=signal.edge_lower_bound,
                    entry_price=signal.entry_price,
                    surface_price=signal.current_outcome_price,
                    surface_kalshi_price=signal.kalshi_yes_price,
                    bought_at=signal.bought_at,
                    recorded_at=_utcnow_iso(),
                    category=signal.category,
                    remaining_edge_status=signal.remaining_edge_status,
                    move_captured_pct=signal.move_captured_pct,
                    conviction=signal.conviction,
                    consensus_wallets=signal.consensus_wallets,
                    wallet_archetype=signal.wallet_archetype,
                    tail_fair_price=signal.tail_fair_price,
                    tail_ev=signal.tail_ev,
                    tail_ev_status=signal.tail_ev_status,
                    tail_ev_source=signal.tail_ev_source,
                    spread_cents=signal.spread_cents,
                    book_status=signal.book_status,
                )
            )
        )

    # 2. Settle.
    resolved = _resolved_outcomes(_load_latest_markets())
    settled_won = settled_lost = settled_void = 0
    for row in rows:
        if str(row.get("status", "open")) != "open":
            continue
        cond = str(row.get("condition_id", ""))
        if cond not in resolved:
            continue
        winner = resolved[cond]
        if winner is None:
            row["status"] = "void"
            row["roi_at_surface"] = 0.0
            row["roi_at_entry"] = 0.0
            settled_void += 1
        else:
            won = int(row.get("outcome_index", 0) or 0) == winner
            row["status"] = "won" if won else "lost"
            row["roi_at_surface"] = _tail_roi(
                won=won, price=float(row.get("surface_price", 0.0) or 0.0)
            )
            row["roi_at_entry"] = _tail_roi(
                won=won, price=float(row.get("entry_price", 0.0) or 0.0)
            )
            if won:
                settled_won += 1
            else:
                settled_lost += 1
        row["settled_at"] = _utcnow_iso()

    if recorded or settled_won or settled_lost or settled_void:
        _write_rows_atomic(path, rows)

    counts = {
        "recorded": recorded,
        "settled_won": settled_won,
        "settled_lost": settled_lost,
        "settled_void": settled_void,
        "total_rows": len(rows),
        "open_rows": sum(1 for r in rows if str(r.get("status", "open")) == "open"),
    }
    log.info("signal ledger updated %s", counts)
    return counts


def list_signal_ledger(
    *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Ledger rows, newest recorded_at first."""
    rows = _read_rows(signal_ledger_path())
    if status:
        rows = [r for r in rows if str(r.get("status", "open")) == status]
    rows.sort(key=lambda r: str(r.get("recorded_at", "")), reverse=True)
    return rows[:limit]


# Dimensions the summary slices settled performance by. Every value was
# frozen on the row at record time; rows recorded before a dimension existed
# fall into the "unknown" bucket.
_BREAKDOWN_DIMENSIONS = (
    "remaining_edge_status",
    "conviction",
    "tradability",
    "category",
    "wallet_archetype",
    "consensus",
    "book_status",
)


def _breakdown_bucket(row: dict[str, Any], dimension: str) -> str:
    if dimension == "consensus":
        n = int(row.get("consensus_wallets", 0) or 0)
        if n <= 0:
            return "unknown"
        if n == 1:
            return "1 wallet"
        if n == 2:
            return "2 wallets"
        return "3+ wallets"
    value = str(row.get(dimension, "") or "")
    return value if value else "unknown"


def _slice_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Performance of one slice of ledger rows. Hit rate alone is meaningless in
    prediction markets (winning 60% of 70c bets is losing), so the slice also
    carries the market-implied win probability at surface time and the
    calibration edge against it:

        avg_market_implied_prob  mean surface price of settled, priced rows
        hit_rate_priced          realized win rate on those same rows
        edge_vs_market           hit_rate_priced − avg_market_implied_prob
                                 (positive → the slice beat the odds the
                                 market quoted when the signal surfaced)
    """
    won = lost = 0
    surface_rois: list[float] = []
    priced_implied: list[float] = []
    priced_wins = 0
    for row in rows:
        status = str(row.get("status", "open"))
        if status not in ("won", "lost"):
            continue
        row_won = status == "won"
        if row_won:
            won += 1
        else:
            lost += 1
        roi_surface = row.get("roi_at_surface")
        if isinstance(roi_surface, (int, float)):
            surface_rois.append(float(roi_surface))
        surface = float(row.get("surface_price", 0.0) or 0.0)
        if 0.0 < surface < 1.0:
            priced_implied.append(surface)
            if row_won:
                priced_wins += 1

    settled = won + lost
    priced = len(priced_implied)
    avg_implied = sum(priced_implied) / priced if priced else 0.0
    hit_priced = priced_wins / priced if priced else 0.0
    return {
        "signals": len(rows),
        "won": won,
        "lost": lost,
        "hit_rate": round(won / settled, 4) if settled else 0.0,
        "avg_roi_at_surface": (
            round(sum(surface_rois) / len(surface_rois), 4) if surface_rois else 0.0
        ),
        "settled_priced": priced,
        "avg_market_implied_prob": round(avg_implied, 4),
        "hit_rate_priced": round(hit_priced, 4),
        "edge_vs_market": round(hit_priced - avg_implied, 4) if priced else 0.0,
    }


def summarize_signal_ledger() -> dict[str, Any]:
    """
    Headline answer to "does tailing the feed work?": hit rate and mean
    per-signal ROI under a flat $1-per-signal staking model, scored at both
    surface price and wallet entry price. Void rows are excluded from ROI.

    Beyond the flat totals the summary carries:
      calibration          did settled signals win more often than their
                           surface price implied? (the market-baseline test)
      open_mark_to_market  unrealized ROI of open rows at the latest observed
                           price, so slow-resolving categories still report
      breakdowns           the same settled stats sliced per quality
                           dimension frozen on each row at record time
    """
    rows = _read_rows(signal_ledger_path())
    counts = {"open": 0, "won": 0, "lost": 0, "void": 0}
    surface_rois: list[float] = []
    entry_rois: list[float] = []
    open_rows: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status", "open"))
        counts[status] = counts.get(status, 0) + 1
        if status == "open":
            open_rows.append(row)
        if status not in ("won", "lost"):
            continue
        roi_surface = row.get("roi_at_surface")
        if isinstance(roi_surface, (int, float)):
            surface_rois.append(float(roi_surface))
        roi_entry = row.get("roi_at_entry")
        if isinstance(roi_entry, (int, float)):
            entry_rois.append(float(roi_entry))

    # Mark open rows to the latest observed price: $1 staked at the surface
    # price is now worth live/surface, so unrealized ROI = live/surface − 1.
    unrealized: list[float] = []
    if open_rows:
        latest_markets = _load_latest_markets()
        for row in open_rows:
            surface = float(row.get("surface_price", 0.0) or 0.0)
            if not 0.0 < surface < 1.0:
                continue
            live = _live_outcome_price(
                latest_markets.get(str(row.get("condition_id", ""))),
                int(row.get("outcome_index", 0) or 0),
            )
            if not 0.0 < live < 1.0:
                continue
            unrealized.append((live - surface) / surface)

    overall = _slice_stats(rows)
    by_dimension: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension in _BREAKDOWN_DIMENSIONS:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(_breakdown_bucket(row, dimension), []).append(row)
        by_dimension[dimension] = {
            bucket: _slice_stats(bucket_rows)
            for bucket, bucket_rows in sorted(grouped.items())
        }

    settled = counts["won"] + counts["lost"]
    return {
        "total_signals": len(rows),
        "open": counts["open"],
        "won": counts["won"],
        "lost": counts["lost"],
        "void": counts["void"],
        "hit_rate": round(counts["won"] / settled, 4) if settled else 0.0,
        "settled_with_surface_price": len(surface_rois),
        "avg_roi_at_surface": (
            round(sum(surface_rois) / len(surface_rois), 4) if surface_rois else 0.0
        ),
        "total_roi_at_surface": round(sum(surface_rois), 4),
        "settled_with_entry_price": len(entry_rois),
        "avg_roi_at_entry": (
            round(sum(entry_rois) / len(entry_rois), 4) if entry_rois else 0.0
        ),
        "total_roi_at_entry": round(sum(entry_rois), 4),
        "calibration": {
            "settled_priced": overall["settled_priced"],
            "avg_market_implied_prob": overall["avg_market_implied_prob"],
            "hit_rate_priced": overall["hit_rate_priced"],
            "edge_vs_market": overall["edge_vs_market"],
        },
        "open_mark_to_market": {
            "marked": len(unrealized),
            "avg_unrealized_roi_at_surface": (
                round(sum(unrealized) / len(unrealized), 4) if unrealized else 0.0
            ),
            "total_unrealized_roi_at_surface": round(sum(unrealized), 4),
        },
        "breakdowns": by_dimension,
    }
