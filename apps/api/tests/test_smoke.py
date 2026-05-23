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
        "/signals/polymarket-leaderboard",
    }.issubset(route_paths)


def test_retired_signal_endpoints_stay_removed() -> None:
    """Regression guard for endpoints that were intentionally removed.

    /signals/leaderboard etc. came from the abandoned Kalshi-user-history
    pipeline. /signals/cross-exchange came from the dislocation framing
    that was reversed on 2026-05-23 (see ADR 0002 — operator is US-based
    and can't trade Polymarket, so a two-leg spread isn't actionable).
    If a stray import resurrects any of these, this test fails."""
    app = create_app()
    route_paths = {route.path for route in app.router.routes if isinstance(route, APIRoute)}
    for retired in (
        "/signals/leaderboard",
        "/signals/orderflow",
        "/signals/opportunities",
        "/signals/trades",
        "/signals/profiles",
        "/signals/cross-exchange",
    ):
        assert retired not in route_paths, (
            f"{retired} should no longer be mounted "
            "(see docs/0002-cross-exchange-decision.md)"
        )
