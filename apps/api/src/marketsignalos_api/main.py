from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from marketsignalos_api.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="MarketSignalOS API",
        version="0.1.0",
    )

    app.include_router(health_router)

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    def metrics() -> PlainTextResponse:
        # Prometheus text exposition format
        data = generate_latest()
        return PlainTextResponse(
            content=data.decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()