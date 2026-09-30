"""Tests for the API's RED instrumentation and feed-serving metrics."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

# pytest's prepend import mode puts this directory on sys.path (there is no
# tests/__init__.py), so the sibling module imports as a top-level name from
# both the repo root and apps/api.
from test_skilled_bets import _seed

from marketsignalos_api.main import app
from marketsignalos_api.observability import (
    PrometheusMiddleware,
    register_pipeline_metrics,
)


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


# ── Label cardinality ────────────────────────────────────────────────────────


def test_request_metrics_use_the_route_template_not_the_raw_path() -> None:
    """A wallet address in a label mints one time series per wallet. The label
    has to be the template so cardinality stays bounded by the route table."""
    # Unknown wallets 404, which is fine here: the assertion is about the
    # `path` label, and three distinct addresses must land on one series.
    template = "/signals/wallets/{proxy_wallet}"
    labels = {"method": "GET", "path": template, "status": "4xx"}
    before = _sample("msos_http_requests_total", labels)

    client = TestClient(app)
    for wallet in ("0xaaa", "0xbbb", "0xccc"):
        client.get(f"/signals/wallets/{wallet}")

    assert _sample("msos_http_requests_total", labels) == before + 3
    # And no series was created under any concrete address.
    for wallet in ("0xaaa", "0xbbb", "0xccc"):
        assert (
            _sample(
                "msos_http_requests_total",
                {"method": "GET", "path": f"/signals/wallets/{wallet}", "status": "4xx"},
            )
            == 0
        )


def test_unmatched_paths_collapse_to_a_single_series() -> None:
    """Otherwise a 404 scan can mint series at will and take the scrape down."""
    labels = {"method": "GET", "path": "<unmatched>", "status": "4xx"}
    before = _sample("msos_http_requests_total", labels)

    client = TestClient(app)
    for path in ("/nope", "/also-nope", "/definitely/not/here"):
        client.get(path)

    assert _sample("msos_http_requests_total", labels) == before + 3


# ── Status classification ────────────────────────────────────────────────────


def test_status_classes_are_bucketed() -> None:
    client = TestClient(app)
    before_ok = _sample(
        "msos_http_requests_total",
        {"method": "GET", "path": "/health", "status": "2xx"},
    )
    client.get("/health")
    assert (
        _sample(
            "msos_http_requests_total",
            {"method": "GET", "path": "/health", "status": "2xx"},
        )
        == before_ok + 1
    )


def test_handler_exceptions_are_counted_as_5xx_and_still_propagate() -> None:
    """An unhandled exception becomes a 500 for the caller, so the error-rate
    SLO must see it. Swallowing it here would under-report the very thing
    ApiErrorRateHigh is computed from."""
    isolated = FastAPI()
    isolated.add_middleware(PrometheusMiddleware)

    @isolated.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("kaboom")

    labels = {"method": "GET", "path": "/boom", "status": "5xx"}
    before = _sample("msos_http_requests_total", labels)

    client = TestClient(isolated, raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 500
    assert _sample("msos_http_requests_total", labels) == before + 1


def test_request_duration_is_recorded() -> None:
    labels = {"method": "GET", "path": "/health"}
    before = _sample("msos_http_request_duration_seconds_count", labels)
    TestClient(app).get("/health")
    assert _sample("msos_http_request_duration_seconds_count", labels) == before + 1


# ── /metrics endpoint ────────────────────────────────────────────────────────


def test_metrics_endpoint_exports_msos_series() -> None:
    client = TestClient(app)
    client.get("/health")
    body = client.get("/metrics").text
    assert "msos_http_requests_total" in body
    assert "msos_http_request_duration_seconds" in body


def test_pipeline_series_exist_from_boot() -> None:
    """Registering pipeline collectors at startup keeps rate() honest and stops
    absent() alerts firing on every fresh deploy: a scrape one second after
    boot should return zeros, not nothing."""
    assert register_pipeline_metrics() is True
    with TestClient(app) as client:  # context manager runs the lifespan
        body = client.get("/metrics").text
    for series in (
        "msos_pipeline_progress_timestamp_seconds",
        "msos_pipeline_running",
        "msos_enrichment_resolved_bets",
        "msos_population_prior_mu",
        "msos_skill_likelihood_saturated_ratio",
    ):
        assert series in body, f"{series} missing from a boot-time scrape"


# ── Feed serving ─────────────────────────────────────────────────────────────


def test_feed_records_compute_then_memory_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three serving layers must be distinguishable: only `compute` can
    take >900 s, so attributing latency to the right one is what makes a
    regression diagnosable rather than merely visible."""
    from marketsignalos_api.services import skilled_bets

    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    skilled_bets.invalidate_cache()

    compute_before = _sample(
        "msos_feed_compute_duration_seconds_count", {"source": "compute"}
    )
    disk_miss_before = _sample(
        "msos_feed_cache_events_total", {"layer": "disk", "result": "miss"}
    )

    skilled_bets.compute_skilled_bets()

    assert (
        _sample("msos_feed_compute_duration_seconds_count", {"source": "compute"})
        == compute_before + 1
    )
    assert (
        _sample("msos_feed_cache_events_total", {"layer": "disk", "result": "miss"})
        == disk_miss_before + 1
    )

    memory_before = _sample(
        "msos_feed_compute_duration_seconds_count", {"source": "memory"}
    )
    hit_before = _sample(
        "msos_feed_cache_events_total", {"layer": "memory", "result": "hit"}
    )

    skilled_bets.compute_skilled_bets()

    assert (
        _sample("msos_feed_compute_duration_seconds_count", {"source": "memory"})
        == memory_before + 1
    )
    assert (
        _sample("msos_feed_cache_events_total", {"layer": "memory", "result": "hit"})
        == hit_before + 1
    )
    skilled_bets.invalidate_cache()


def test_feed_records_a_disk_hit_when_the_memo_is_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold process with a warm spill artifact is the case that took the feed
    from >15 min to 0.6 s, so it needs its own label rather than folding into
    the compute path."""
    from marketsignalos_api.services import skilled_bets

    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    skilled_bets.invalidate_cache()

    skilled_bets.compute_skilled_bets()  # populates both memo and disk artifact
    # Drop only the in-memory memo, as a process restart would.
    skilled_bets._compute_skilled_bets_cached.cache_clear()

    disk_hit_before = _sample(
        "msos_feed_cache_events_total", {"layer": "disk", "result": "hit"}
    )
    disk_source_before = _sample(
        "msos_feed_compute_duration_seconds_count", {"source": "disk"}
    )

    skilled_bets.compute_skilled_bets()

    assert (
        _sample("msos_feed_cache_events_total", {"layer": "disk", "result": "hit"})
        == disk_hit_before + 1
    )
    assert (
        _sample("msos_feed_compute_duration_seconds_count", {"source": "disk"})
        == disk_source_before + 1
    )
    skilled_bets.invalidate_cache()


def test_feed_signal_gauge_tracks_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marketsignalos_api.services import skilled_bets

    pm_dir = _seed(tmp_path)
    monkeypatch.setenv("POLYMARKET_DATA_DIR", str(pm_dir))
    skilled_bets.invalidate_cache()

    signals = skilled_bets.compute_skilled_bets(limit=None)
    assert _sample("msos_feed_signals") == len(signals)
    skilled_bets.invalidate_cache()
