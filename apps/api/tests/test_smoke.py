from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from marketsignalos_api.main import create_app


def test_app_factory_returns_fastapi_app() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_app_registers_active_routes() -> None:
    """Only the surfaces that serve the Polymarket-→-Kalshi mapping goal
    should be mounted. Kalshi user-history endpoints were retired (see
    docs/0002-cross-exchange-decision.md)."""
    app = create_app()
    route_paths = {route.path for route in app.router.routes if isinstance(route, APIRoute)}
    assert {
        "/",
        "/health",
        "/metrics",
        "/ingestor/run",
        "/ingestor/status",
        "/signals/skilled-bets",
        "/signals/cross-exchange",
        "/signals/polymarket-leaderboard",
    }.issubset(route_paths)


def test_kalshi_user_history_endpoints_removed() -> None:
    """Regression guard: these routes used to exist but were tied to the
    abandoned Kalshi-user-history pipeline. If a stray import resurrects
    them, this test fails and points the reader at the ADR."""
    app = create_app()
    route_paths = {route.path for route in app.router.routes if isinstance(route, APIRoute)}
    for retired in (
        "/signals/leaderboard",
        "/signals/orderflow",
        "/signals/opportunities",
        "/signals/trades",
        "/signals/profiles",
    ):
        assert retired not in route_paths, (
            f"{retired} should no longer be mounted "
            "(see docs/0002-cross-exchange-decision.md)"
        )
