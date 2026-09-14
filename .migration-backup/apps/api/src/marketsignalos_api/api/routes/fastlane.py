from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from marketsignalos_api.services.fastlane import fastlane_status, run_fastlane_tick

router = APIRouter(prefix="/signals/fastlane", tags=["signals"])


@router.get("/status")
def get_fastlane_status() -> dict[str, Any]:
    """Fast-lane poller state: config (when the loop is on), persisted
    watermarks, and the last tick's outcome."""
    return fastlane_status()


@router.post("/run")
def trigger_fastlane_tick() -> dict[str, Any]:
    """Run one fast-lane polling pass immediately.

    Exists for manual checks and for deployments that prefer an external
    scheduler over the in-process loop. Synchronous — the work is one
    activity-page fetch per watched wallet plus at most one webhook POST.
    """
    return run_fastlane_tick()
