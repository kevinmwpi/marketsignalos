"""Prometheus instrumentation owned by the API process.

Split from the ingestor's ``marketsignalos_polymarket.metrics`` along the
same seam the packages already use: pipeline, upstream-client and model-health
series belong to the ingestor, request and feed-serving series belong here.
Both register on the default registry, so ``/metrics`` exports the union.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("marketsignalos.observability")

# Request latency spans a trivial /health and a cold feed recompute, so the
# buckets have to reach far past a normal web SLO to stay useful.
_REQUEST_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0)

# Feed recompute: 0.6 s served from the disk artifact, >900 s walking the
# activity file cold (commit 7874521).
_FEED_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0)


http_requests_total = Counter(
    "msos_http_requests_total",
    "HTTP requests by method, route template and status class.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "msos_http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "path"],
    buckets=_REQUEST_BUCKETS,
)

feed_compute_duration_seconds = Histogram(
    "msos_feed_compute_duration_seconds",
    "Time to produce a skilled-bets feed result, labelled by how it was served.",
    ["source"],
    buckets=_FEED_BUCKETS,
)

feed_cache_events_total = Counter(
    "msos_feed_cache_events_total",
    "Skilled-bets feed cache lookups by layer and result.",
    ["layer", "result"],
)

feed_signals = Gauge(
    "msos_feed_signals",
    "Signals in the most recently computed skilled-bets feed.",
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """RED metrics for every request.

    Labels use the *route template* (``/signals/wallets/{proxy_wallet}``), read
    from the ASGI scope after routing, never the raw path. A wallet address in
    a label would mint one time series per address and take the registry — and
    then the scrape — down with it. Unmatched paths collapse to a single
    ``<unmatched>`` series for the same reason: a 404 scan must not be able to
    create series at will.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled handler exception still becomes a 500 downstream;
            # record it rather than letting the series silently under-count the
            # error rate the SLO alert is computed from.
            self._observe(request, started, "5xx")
            raise
        self._observe(request, started, _status_class(response.status_code))
        return response

    def _observe(self, request: Request, started: float, status: str) -> None:
        path = _route_template(request)
        method = request.method
        http_request_duration_seconds.labels(method=method, path=path).observe(
            time.perf_counter() - started
        )
        http_requests_total.labels(method=method, path=path, status=status).inc()


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "<unmatched>"


def register_pipeline_metrics() -> bool:
    """Import the ingestor's collectors so their series exist from boot.

    The pipeline runs in this process, but only behind a call-time import, so
    without this every pipeline series would be missing from ``/metrics`` until
    the first ingest. That is worse than it sounds: ``rate()`` over a series
    that springs into existence mid-window is wrong, and ``absent()`` alerts
    would fire on every fresh deploy. Registering at boot means a scrape one
    second after start returns honest zeros.

    The ingestor is a sibling package rather than a declared dependency, so a
    failed import degrades to request-only metrics instead of blocking boot.
    """
    try:
        import marketsignalos_polymarket.metrics  # noqa: F401
    except ImportError:
        log.warning(
            "pipeline metrics unavailable: marketsignalos_polymarket not importable; "
            "/metrics will export request metrics only"
        )
        return False
    return True
