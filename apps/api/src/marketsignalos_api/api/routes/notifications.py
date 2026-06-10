from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from marketsignalos_api.services.notifications import (
    notification_status,
    run_notification_pass,
)

router = APIRouter(prefix="/signals/notifications", tags=["signals"])


@router.get("/status")
def get_notification_status() -> dict[str, Any]:
    """Webhook configuration state and delivery watermarks."""
    return notification_status()


@router.post("/run")
def trigger_notification_pass() -> dict[str, Any]:
    """Deliver any signals/exits newer than the stored watermarks to the
    webhook configured via SIGNAL_WEBHOOK_URL.

    Runs automatically after every API-triggered ingest; this endpoint exists
    for CLI-driven ingests and manual checks.
    """
    return run_notification_pass()
