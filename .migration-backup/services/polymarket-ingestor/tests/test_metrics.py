"""Tests for the pipeline instrumentation.

These assert the properties the alert rules depend on. An alert is only as
good as the series behind it, so the invariants worth pinning are the ones a
refactor could quietly break: label cardinality, the progress heartbeat
actually advancing, and the cookie-jar gauge being sampled before the clear
rather than after.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY

from marketsignalos_polymarket import metrics
from marketsignalos_polymarket.polymarket_client import (
    LB_API,
    PolymarketClient,
    PolymarketClientConfig,
)
from marketsignalos_polymarket.rate_limiter import HostRateLimiter


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _client_with_mock(handler: Any) -> PolymarketClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, headers={"Accept": "application/json"})
    return PolymarketClient(
        config=PolymarketClientConfig(max_retries=2, retry_backoff_seconds=0.001),
        client=http,
    )


# ── Label cardinality ────────────────────────────────────────────────────────
# A wallet address reaching a metric label mints one time series per wallet and
# eventually takes the scrape down with it, so the normalizer is load-bearing.


@pytest.mark.parametrize(
    ("url", "expected_host", "expected_path"),
    [
        (
            "https://data-api.polymarket.com/activity?user=0xabc",
            "data-api.polymarket.com",
            "/activity",
        ),
        (
            "https://data-api.polymarket.com/positions",
            "data-api.polymarket.com",
            "/positions",
        ),
        ("https://lb-api.polymarket.com/profit", "lb-api.polymarket.com", "/profit"),
        (
            "https://gamma-api.polymarket.com/markets/0x1234567890abcdef",
            "gamma-api.polymarket.com",
            "/markets/:id",
        ),
        (
            "https://gamma-api.polymarket.com/markets/512345",
            "gamma-api.polymarket.com",
            "/markets/:id",
        ),
    ],
)
def test_split_endpoint_collapses_identifiers(
    url: str, expected_host: str, expected_path: str
) -> None:
    host, path = metrics.split_endpoint(url)
    assert host == expected_host
    assert path == expected_path


def test_split_endpoint_is_stable_across_distinct_wallets() -> None:
    """Different wallets must not produce different label values."""
    paths = {
        metrics.split_endpoint(
            f"https://data-api.polymarket.com/activity?user=0x{n:040x}"
        )[1]
        for n in range(50)
    }
    assert paths == {"/activity"}


# ── Progress heartbeat (2026-07-13) ──────────────────────────────────────────


def test_heartbeat_advances_progress_gauge() -> None:
    metrics.pipeline_progress_timestamp_seconds.set(0)
    metrics.heartbeat()
    assert _sample("msos_pipeline_progress_timestamp_seconds") > 0


def test_stage_records_duration_and_stamps_progress() -> None:
    metrics.pipeline_progress_timestamp_seconds.set(0)
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "unit-test-stage"}
    )
    with metrics.stage("unit-test-stage"):
        pass
    after = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "unit-test-stage"}
    )
    assert after == before + 1
    assert _sample("msos_pipeline_progress_timestamp_seconds") > 0


def test_begin_stage_closes_the_previous_stage() -> None:
    """Stage timings are derived from transitions, so opening B is what closes
    A. This is the mechanism the whole stage histogram rests on."""
    before_a = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "transition-a"}
    )
    metrics.begin_stage("transition-a")
    metrics.begin_stage("transition-b")
    assert (
        _sample("msos_pipeline_stage_duration_seconds_count", {"stage": "transition-a"})
        == before_a + 1
    )
    metrics.end_stage()


def test_reentering_the_open_stage_does_not_restart_the_timer() -> None:
    """Progress updates ("wallet 37 of 400") re-emit the same stage name. They
    must count as liveness without splitting one stage into hundreds of
    near-zero observations."""
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "repeat-stage"}
    )
    metrics.begin_stage("repeat-stage")
    for _ in range(5):
        metrics.begin_stage("repeat-stage")
    assert (
        _sample("msos_pipeline_stage_duration_seconds_count", {"stage": "repeat-stage"})
        == before
    )
    metrics.end_stage()
    assert (
        _sample("msos_pipeline_stage_duration_seconds_count", {"stage": "repeat-stage"})
        == before + 1
    )


def test_end_stage_is_idempotent() -> None:
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "idempotent-stage"}
    )
    metrics.begin_stage("idempotent-stage")
    metrics.end_stage()
    metrics.end_stage()
    metrics.end_stage()
    assert (
        _sample(
            "msos_pipeline_stage_duration_seconds_count", {"stage": "idempotent-stage"}
        )
        == before + 1
    )


def test_track_run_closes_the_final_stage() -> None:
    """The last stage of a run has no successor to close it. Without the close
    in track_run.__exit__ the most expensive stage of every run (enrichment)
    would never be observed at all."""
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "final-stage"}
    )
    with metrics.track_run("unit-final"):
        metrics.begin_stage("final-stage")
    assert (
        _sample("msos_pipeline_stage_duration_seconds_count", {"stage": "final-stage"})
        == before + 1
    )


def test_track_run_closes_the_final_stage_even_when_the_run_raises() -> None:
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "final-stage-raising"}
    )
    with pytest.raises(RuntimeError), metrics.track_run("unit-final-raising"):
        metrics.begin_stage("final-stage-raising")
        raise RuntimeError("boom")
    assert (
        _sample(
            "msos_pipeline_stage_duration_seconds_count",
            {"stage": "final-stage-raising"},
        )
        == before + 1
    )


def test_stage_records_duration_even_when_the_body_raises() -> None:
    before = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "raising-stage"}
    )
    with pytest.raises(RuntimeError), metrics.stage("raising-stage"):
        raise RuntimeError("boom")
    after = _sample(
        "msos_pipeline_stage_duration_seconds_count", {"stage": "raising-stage"}
    )
    assert after == before + 1


# ── Run tracking ─────────────────────────────────────────────────────────────


def test_track_run_records_success_and_clears_running_flag() -> None:
    before = _sample(
        "msos_pipeline_runs_total", {"kind": "unit-success", "outcome": "success"}
    )
    with metrics.track_run("unit-success"):
        assert _sample("msos_pipeline_running") == 1
    assert _sample("msos_pipeline_running") == 0
    assert (
        _sample(
            "msos_pipeline_runs_total", {"kind": "unit-success", "outcome": "success"}
        )
        == before + 1
    )
    assert (
        _sample(
            "msos_pipeline_last_success_timestamp_seconds", {"kind": "unit-success"}
        )
        > 0
    )


def test_track_run_records_failure_and_clears_running_flag() -> None:
    """A wedged run that raises must not leave pipeline_running pinned at 1 —
    PipelineStalled is gated on that flag, so a leak here would make the alert
    fire forever on an idle deployment."""
    before = _sample(
        "msos_pipeline_runs_total", {"kind": "unit-failure", "outcome": "failure"}
    )
    with pytest.raises(ValueError), metrics.track_run("unit-failure"):
        raise ValueError("boom")
    assert _sample("msos_pipeline_running") == 0
    assert (
        _sample(
            "msos_pipeline_runs_total", {"kind": "unit-failure", "outcome": "failure"}
        )
        == before + 1
    )
    assert (
        _sample(
            "msos_pipeline_last_success_timestamp_seconds", {"kind": "unit-failure"}
        )
        == 0
    )


# ── Client instrumentation (2026-07-10) ──────────────────────────────────────


def test_request_records_build_and_transport_separately() -> None:
    labels = {"host": "lb-api.polymarket.com", "endpoint": "/profit"}
    build_before = _sample(
        "msos_upstream_request_build_seconds_count", {"host": "lb-api.polymarket.com"}
    )
    duration_before = _sample("msos_upstream_request_duration_seconds_count", labels)
    total_before = _sample(
        "msos_upstream_requests_total", {**labels, "outcome": "success"}
    )

    client = _client_with_mock(lambda r: httpx.Response(200, json=[]))
    client.get_leaderboard(metric="profit", window="all", limit=1)
    client.close()

    assert (
        _sample(
            "msos_upstream_request_build_seconds_count",
            {"host": "lb-api.polymarket.com"},
        )
        == build_before + 1
    )
    assert (
        _sample("msos_upstream_request_duration_seconds_count", labels)
        == duration_before + 1
    )
    assert (
        _sample("msos_upstream_requests_total", {**labels, "outcome": "success"})
        == total_before + 1
    )


def test_cookie_jar_gauge_is_sampled_before_the_clear() -> None:
    """The jar is cleared after every response, so a gauge read after the clear
    would report zero forever and silently stop guarding the O(n^2) regression
    it exists to catch. It must observe what the response actually set."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers=[
                ("set-cookie", "__cf_bm=a; Path=/"),
                ("set-cookie", "__cf_other=b; Path=/"),
            ],
        )

    client = _client_with_mock(handler)
    client.get_leaderboard(metric="profit", window="all", limit=1)
    assert _sample("msos_http_client_cookie_jar_size") == 2
    # ...and the jar itself is still emptied, which is the actual fix.
    assert len(client._client.cookies.jar) == 0
    client.close()


def test_retryable_status_increments_retry_counter() -> None:
    labels = {
        "host": "lb-api.polymarket.com",
        "endpoint": "/profit",
        "reason": "server_error",
    }
    before = _sample("msos_upstream_retries_total", labels)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_leaderboard(metric="profit", window="all", limit=1)
    client.close()
    assert _sample("msos_upstream_retries_total", labels) == before + 1


def test_transport_error_records_a_transport_outcome() -> None:
    labels = {
        "host": "lb-api.polymarket.com",
        "endpoint": "/profit",
        "outcome": "transport_error",
    }
    before = _sample("msos_upstream_requests_total", labels)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("nope")

    client = _client_with_mock(handler)
    with pytest.raises(httpx.TransportError):
        client.get_leaderboard(metric="profit", window="all", limit=1)
    client.close()
    # One per attempt: the initial call plus max_retries retries.
    assert _sample("msos_upstream_requests_total", labels) == before + 3


def test_client_still_targets_the_right_url_after_the_build_send_split() -> None:
    """The split from client.get() to build_request()+send() must be
    behaviour-preserving, params and all."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=[])

    client = _client_with_mock(handler)
    client.get_leaderboard(metric="volume", window="week", limit=7)
    client.close()
    assert seen["method"] == "GET"
    assert seen["url"] == f"{LB_API}/volume?window=week&limit=7"


# ── Rate limiter ─────────────────────────────────────────────────────────────


def test_rate_limiter_observes_wait_time() -> None:
    before = _sample("msos_rate_limiter_wait_seconds_count")
    limiter = HostRateLimiter(rps=1000.0)
    limiter.wait()
    limiter.wait()
    assert _sample("msos_rate_limiter_wait_seconds_count") == before + 2


def test_rate_limiter_still_spaces_requests() -> None:
    """Instrumentation must not change the limiter's actual behaviour."""
    import time as _time

    limiter = HostRateLimiter(rps=50.0)
    limiter.wait()
    started = _time.monotonic()
    limiter.wait()
    assert _time.monotonic() - started >= 0.015


# ── RSS ──────────────────────────────────────────────────────────────────────


def test_process_rss_bytes_is_plausible_or_absent() -> None:
    rss = metrics.process_rss_bytes()
    if rss is None:  # Windows dev boxes have neither source
        pytest.skip("no RSS source on this platform")
    assert rss > 1_000_000
