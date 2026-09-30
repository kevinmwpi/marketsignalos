from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from marketsignalos_api.services.exit_signals import list_exit_signals, update_exit_signals

router = APIRouter(prefix="/signals/exits", tags=["signals"])


@router.get("")
def get_exit_signals(
    exit_type: Literal["closed", "trimmed"] | None = Query(
        default=None, description="Filter by exit type."
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Skilled wallets that closed or materially trimmed a still-open
    position (un-tail signals), newest first."""
    return list_exit_signals(exit_type=exit_type, limit=limit)


@router.post("/refresh")
def refresh_exit_signals() -> dict[str, int]:
    """Diff each skilled wallet's latest complete position snapshot against
    the last one examined and record new exits.

    Runs automatically after every API-triggered ingest; this endpoint exists
    for CLI-driven ingests and manual checks.
    """
    return update_exit_signals()
