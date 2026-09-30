from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from marketsignalos_api.services.signal_ledger import (
    list_signal_ledger,
    summarize_signal_ledger,
    update_signal_ledger,
)

router = APIRouter(prefix="/signals/ledger", tags=["signals"])


@router.get("")
def get_signal_ledger(
    status: Literal["open", "won", "lost", "void"] | None = Query(
        default=None, description="Filter rows by settlement status."
    ),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Recorded feed signals (paper-trade ledger), newest first."""
    return list_signal_ledger(status=status, limit=limit)


@router.get("/summary")
def get_signal_ledger_summary() -> dict[str, Any]:
    """Hit rate and hypothetical $1-per-signal tail ROI across settled signals."""
    return summarize_signal_ledger()


@router.post("/refresh")
def refresh_signal_ledger() -> dict[str, int]:
    """Record newly surfaced signals and settle any whose market resolved.

    Runs automatically after every API-triggered ingest; this endpoint exists
    for CLI-driven ingests and manual checks. Synchronous — the work is two
    JSONL reads and an atomic rewrite.
    """
    return update_signal_ledger()
