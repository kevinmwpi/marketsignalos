from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

from marketsignalos_api.services.platform_status import public_status


router = APIRouter()


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/platform/status")
def platform_status() -> dict[str, Any]:
    """Public freshness and coverage, separate from process liveness."""
    return public_status()
