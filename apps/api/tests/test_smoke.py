from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from marketsignalos_api.main import create_app


def _route_paths(app: FastAPI) -> set[str]:
    """All mounted APIRoute paths. FastAPI ≥0.139 keeps included routers
    wrapped in a lazy _IncludedRouter entry (exposing the underlying
    APIRouter as `original_router`) instead of flattening their routes into
    app.router.routes, so enumerate both shapes."""
    paths: set[str] = set()
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            paths.add(route.path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            paths.update(r.path for r in inner.routes if isinstance(r, APIRoute))
    return paths


def test_app_factory_returns_fastapi_app() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_app_registers_active_routes() -> None:
    """Only the surfaces that serve the Polymarket-→-Kalshi mapping goal
    should be mounted. Kalshi user-history endpoints were retired (see
    docs/0002-cross-exchange-decision.md)."""
    route_paths = _route_paths(create_app())
    assert {
        "/",
        "/health",
        "/metrics",
        "/ingestor/run",
        "/ingestor/status",
        "/signals/skilled-bets",
        "/signals/skilled-bets/summary",
        "/signals/polymarket-leaderboard",
    }.issubset(route_paths)


def test_retired_signal_endpoints_stay_removed() -> None:
    """Regression guard for endpoints that were intentionally removed.

    /signals/leaderboard etc. came from the abandoned Kalshi-user-history
    pipeline. /signals/cross-exchange came from the dislocation framing
    that was reversed on 2026-05-23 (see ADR 0002 — operator is US-based
    and can't trade Polymarket, so a two-leg spread isn't actionable).
    If a stray import resurrects any of these, this test fails."""
    route_paths = _route_paths(create_app())
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
