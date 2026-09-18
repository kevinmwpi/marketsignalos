"""Expose the imported API under Replit's /api path in preview and production."""
from __future__ import annotations

import os
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

# Replit preserved the imported project here. Do not depend on a workspace's
# absolute /home/runner path or accidentally import a different editable install.
ROOT = Path(__file__).resolve().parents[2]
for source in (ROOT / ".migration-backup/apps/api/src",
               ROOT / ".migration-backup/services/polymarket-ingestor/src"):
    sys.path.insert(0, str(source))

from marketsignalos_api._paths import polymarket_positions_path
from marketsignalos_api.main import app as imported_app


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # A fresh publication has no data directory yet; public feed caching needs it.
    polymarket_positions_path().parent.mkdir(parents=True, exist_ok=True)
    # ASGI mounts do not automatically run the mounted app's lifespan.
    async with imported_app.router.lifespan_context(imported_app):
        yield


app = FastAPI(title="MarketSignalOS API", lifespan=lifespan)


@app.middleware("http")
async def protect_operators(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """The imported older main predates operator auth on the budget feature branch."""
    private = request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path.rstrip("/") in {
        "/api/ingestor/status", "/api/ingestor/watchlist", "/api/metrics",
        "/api/signals/notifications/status", "/api/signals/fastlane/status",
    }
    if private:
        token = os.getenv("ADMIN_API_TOKEN", "").strip()
        if len(token) < 32:
            return JSONResponse({"detail": "Operator access is not configured"}, status_code=503)
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse({"detail": "Operator authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


@app.get("/api/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


app.mount("/api", imported_app)
