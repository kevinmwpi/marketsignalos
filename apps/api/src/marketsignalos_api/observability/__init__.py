"""API-side observability: RED instrumentation and feed health metrics."""
from __future__ import annotations

from marketsignalos_api.observability.metrics import (
    PrometheusMiddleware,
    feed_cache_events_total,
    feed_compute_duration_seconds,
    feed_signals,
    http_request_duration_seconds,
    http_requests_total,
    register_pipeline_metrics,
)

__all__ = [
    "PrometheusMiddleware",
    "feed_cache_events_total",
    "feed_compute_duration_seconds",
    "feed_signals",
    "http_request_duration_seconds",
    "http_requests_total",
    "register_pipeline_metrics",
]
