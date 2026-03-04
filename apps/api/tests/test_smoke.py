from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from marketsignalos_api.main import create_app


def test_app_factory_returns_fastapi_app() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_app_registers_core_routes() -> None:
    app = create_app()
    route_paths = {route.path for route in app.router.routes if isinstance(route, APIRoute)}
    assert "/health" in route_paths
    assert "/metrics" in route_paths
