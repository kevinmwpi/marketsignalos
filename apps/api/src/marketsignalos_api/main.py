from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from marketsignalos_api.api.routes.exit_signals import router as exit_signals_router
from marketsignalos_api.api.routes.fastlane import router as fastlane_router
from marketsignalos_api.api.routes.health import router as health_router
from marketsignalos_api.api.routes.ingestor import router as ingestor_router
from marketsignalos_api.api.routes.market_consensus import router as market_consensus_router
from marketsignalos_api.api.routes.notifications import router as notifications_router
from marketsignalos_api.api.routes.polymarket import router as polymarket_router
from marketsignalos_api.api.routes.signal_ledger import router as signal_ledger_router
from marketsignalos_api.api.routes.skilled_bets import router as skilled_bets_router
from marketsignalos_api.api.routes.wallets import router as wallets_router
from marketsignalos_api.observability import (
    PrometheusMiddleware,
    register_pipeline_metrics,
)
from marketsignalos_api.services.fastlane import start_fastlane_from_env, stop_fastlane
from marketsignalos_api.services.ingest_scheduler import (
    start_scheduler_from_env,
    stop_scheduler,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the optional background loops (ingest scheduler + fast-lane
    poller) with the server and cancel them cleanly on shutdown. Both are
    no-ops unless their env vars are set (INGEST_EVERY_MINUTES /
    FASTLANE_EVERY_SECONDS).

    Pipeline collectors are registered here too so every series exists from
    boot rather than appearing after the first ingest."""
    register_pipeline_metrics()
    start_scheduler_from_env()
    start_fastlane_from_env()
    try:
        yield
    finally:
        await stop_scheduler()
        await stop_fastlane()


def _landing_page_html() -> str:
    """Minimal API landing page. The product dashboard lives in the Next.js
    app (apps/web); this page just orients direct callers of the API."""
    frontend_url = os.getenv("FRONTEND_URL", "").strip()
    dashboard_link = (
        f'<a class="btn primary" href="{frontend_url}">Open dashboard</a>'
        if frontend_url
        else ""
    )
    return f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>MarketSignalOS API</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
        background: #09090b;
        color: #f4f4f5;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
      }}
      .card {{
        max-width: 560px;
        padding: 28px 32px;
        border: 1px solid rgba(161,161,170,0.25);
        border-radius: 16px;
        background: rgba(24,24,27,0.92);
      }}
      h1 {{ margin: 0 0 8px; font-size: 1.6rem; }}
      p  {{ color: #a1a1aa; line-height: 1.6; margin: 8px 0; }}
      .actions {{ margin-top: 18px; display: flex; flex-wrap: wrap; gap: 10px; }}
      .btn {{
        display: inline-flex; align-items: center; padding: 8px 14px;
        border-radius: 999px; text-decoration: none; font-weight: 600;
        border: 1px solid rgba(161,161,170,0.3); color: #f4f4f5;
      }}
      .btn.primary {{ background: #34d399; color: #052e16; border-color: transparent; }}
      code {{ background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>MarketSignalOS API</h1>
      <p>
        Identifies skilled Polymarket wallets and maps their active positions
        to equivalent Kalshi markets. The user-facing dashboard runs in a
        separate Next.js app; this URL serves the JSON API only.
      </p>
      <p>
        Primary endpoints: <code>/signals/skilled-bets</code>,
        <code>/signals/polymarket-leaderboard</code>,
        <code>/ingestor/run</code>.
      </p>
      <div class="actions">
        {dashboard_link}
        <a class="btn" href="/docs">API docs</a>
        <a class="btn" href="/health">Health</a>
      </div>
    </div>
  </body>
</html>
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="MarketSignalOS API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # Registered before CORS so it is the outermost middleware and therefore
    # times the whole request, CORS handling included — instrumentation that
    # measures only part of the stack quietly under-reports latency.
    app.add_middleware(PrometheusMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(exit_signals_router)
    app.include_router(fastlane_router)
    app.include_router(health_router)
    app.include_router(ingestor_router)
    app.include_router(market_consensus_router)
    app.include_router(notifications_router)
    app.include_router(polymarket_router)
    app.include_router(signal_ledger_router)
    app.include_router(skilled_bets_router)
    app.include_router(wallets_router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        return HTMLResponse(content=_landing_page_html())

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    def metrics() -> PlainTextResponse:
        data = generate_latest()
        return PlainTextResponse(
            content=data.decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()
