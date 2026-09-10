"""Public reads and explicitly authorized operator actions."""
from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request


def require_operator(request: Request) -> None:
    protected = request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path in {
        "/ingestor/status", "/ingestor/watchlist", "/signals/notifications/status",
        "/signals/fastlane/status", "/metrics",
    }
    if not protected:
        return
    token = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not token:
        if (
            os.getenv("ALLOW_UNAUTHENTICATED_ADMIN") == "1"
            and os.getenv("APP_ENV", "development") != "production"
            and not os.getenv("RAILWAY_ENVIRONMENT_ID")
        ):
            return
        raise HTTPException(status_code=503, detail="Operator access is not configured")
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        supplied.encode(), token.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Operator authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
