"""
Wallet detail aggregation — everything the score asserts, made inspectable.

The dashboard asserts "97.4% forecast confidence" per wallet; this service
assembles the evidence behind that number in one response: the full
enrichment row, still-held positions, the per-position bet history written
at enrichment time (with CLV), an equity curve over settled bets, a category
breakdown, and the wallet's own paper-trade ledger and exit rows.

Everything is read from the JSONL stores the pipeline already maintains —
no recomputation, no extra activity-file scans.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from marketsignalos_api._paths import (
    polymarket_enrichment_path,
    polymarket_wallet_bets_path,
    polymarket_wallet_values_path,
)
from marketsignalos_api.services.exit_signals import list_exit_signals
from marketsignalos_api.services.external_urls import polymarket_profile_url
from marketsignalos_api.services.signal_ledger import list_signal_ledger
from marketsignalos_api.services.skilled_bets import _f, _read_jsonl, compute_skilled_bets


def _load_enrichment_row(wallet: str) -> dict[str, Any] | None:
    for row in _read_jsonl(polymarket_enrichment_path()):
        if str(row.get("proxy_wallet", "")).lower() == wallet:
            return row
    return None


def _load_wallet_bets(wallet: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in _read_jsonl(polymarket_wallet_bets_path())
        if str(row.get("proxy_wallet", "")).lower() == wallet
    ]
    rows.sort(key=lambda r: int(r.get("last_trade_ts", 0) or 0), reverse=True)
    return rows


def _latest_wallet_value(wallet: str) -> float:
    best: tuple[str, float] | None = None
    for row in _read_jsonl(polymarket_wallet_values_path()):
        if str(row.get("proxy_wallet", "")).lower() != wallet:
            continue
        snapshot_at = str(row.get("snapshot_at", "") or "")
        if best is None or snapshot_at >= best[0]:
            best = (snapshot_at, _f(row.get("value_usdc")))
    return best[1] if best else 0.0


def _equity_curve(bets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cumulative PnL over settled (won/lost/exited) bets, oldest first."""
    settled = [
        b
        for b in bets
        if str(b.get("status", "")) in ("won", "lost", "exited")
        and int(b.get("last_trade_ts", 0) or 0) > 0
    ]
    settled.sort(key=lambda b: int(b.get("last_trade_ts", 0) or 0))
    curve: list[dict[str, Any]] = []
    cumulative = 0.0
    for bet in settled:
        cumulative += _f(bet.get("total_pnl_usdc"))
        curve.append(
            {
                "ts": int(bet.get("last_trade_ts", 0) or 0),
                "cumulative_pnl_usdc": round(cumulative, 2),
            }
        )
    return curve


def _category_breakdown(bets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category: dict[str, dict[str, Any]] = {}
    for bet in bets:
        category = str(bet.get("category", "") or "uncategorized")
        agg = by_category.setdefault(
            category,
            {
                "category": category,
                "bets": 0,
                "wins": 0,
                "losses": 0,
                "open": 0,
                "pnl_usdc": 0.0,
            },
        )
        agg["bets"] += 1
        status = str(bet.get("status", ""))
        if status == "won":
            agg["wins"] += 1
        elif status == "lost":
            agg["losses"] += 1
        elif status == "open":
            agg["open"] += 1
        agg["pnl_usdc"] = round(agg["pnl_usdc"] + _f(bet.get("total_pnl_usdc")), 2)
    out = list(by_category.values())
    for agg in out:
        resolved = agg["wins"] + agg["losses"]
        agg["win_rate"] = round(agg["wins"] / resolved, 4) if resolved else 0.0
    out.sort(key=lambda a: (-a["bets"], a["category"]))
    return out


def compute_wallet_detail(
    wallet: str, *, bet_limit: int = 100
) -> dict[str, Any] | None:
    """Full wallet dossier, or None when the wallet has no enrichment row."""
    wallet = wallet.lower()
    enrichment = _load_enrichment_row(wallet)
    if enrichment is None:
        return None

    bets = _load_wallet_bets(wallet)
    open_positions = [
        asdict(signal)
        for signal in compute_skilled_bets(include_untradable=True, limit=None)
        if signal.proxy_wallet == wallet
    ]
    ledger_rows = [
        row
        for row in list_signal_ledger(limit=1000)
        if str(row.get("proxy_wallet", "")).lower() == wallet
    ]
    exit_rows = [
        row
        for row in list_exit_signals(limit=500)
        if str(row.get("proxy_wallet", "")).lower() == wallet
    ]

    return {
        "proxy_wallet": wallet,
        "polymarket_profile_url": polymarket_profile_url(wallet),
        "wallet_value_usdc": round(_latest_wallet_value(wallet), 2),
        "enrichment": enrichment,
        "open_positions": open_positions,
        "bet_history": bets[:bet_limit],
        "bet_history_total": len(bets),
        "equity_curve": _equity_curve(bets),
        "category_breakdown": _category_breakdown(bets),
        "ledger": ledger_rows,
        "exits": exit_rows,
    }
