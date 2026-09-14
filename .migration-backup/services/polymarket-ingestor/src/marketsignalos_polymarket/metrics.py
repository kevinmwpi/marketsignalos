"""Prometheus instrumentation for the Polymarket pipeline.

Collectors register on the prometheus_client *default* registry. The pipeline
runs in-process inside the API (``/ingestor/run`` and the background
scheduler), so the API's ``/metrics`` endpoint exports these series without
any extra plumbing. Importing this module is enough to create the series —
the API does so eagerly at boot so a scrape before the first ingest returns
zeros rather than nothing at all (``absent()`` and ``rate()`` both behave
badly against a series that only appears after the first run).

Deliberately import-light: stdlib plus prometheus_client, no runner imports.

Every collector here exists because something broke in production. See
``docs/observability.md`` for the incident-to-alert mapping.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from prometheus_client import Counter, Gauge, Histogram

# ── Bucket layouts ───────────────────────────────────────────────────────────
# Chosen against observed production ranges, not defaults. A histogram whose
# top bucket sits below the pathological case cannot distinguish "slow" from
# "hung", which is exactly the distinction each incident turned on.

# Upstream calls: sub-second normally, minutes when Polymarket is degraded.
_HTTP_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

# Request *construction*: microseconds normally. The 2026-07-10 cookie-jar
# bug pushed this to minutes-per-request while sockets stayed healthy, so the
# range has to span five orders of magnitude to show the regression at all.
_BUILD_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0)

# Pipeline stages run from seconds (leaderboard seed) to hours (deep sweep).
_STAGE_BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 21600.0)

# Rate-limiter spacing: one token at 5 rps is 0.2 s; anything past a second
# means workers are queueing behind the shared budget.
_WAIT_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


# ── Pipeline lifecycle ───────────────────────────────────────────────────────

pipeline_runs_total = Counter(
    "msos_pipeline_runs_total",
    "Pipeline runs by kind and terminal outcome.",
    ["kind", "outcome"],
)

pipeline_stage_duration_seconds = Histogram(
    "msos_pipeline_stage_duration_seconds",
    "Wall-clock duration of each pipeline stage.",
    ["stage"],
    buckets=_STAGE_BUCKETS,
)

pipeline_last_success_timestamp_seconds = Gauge(
    "msos_pipeline_last_success_timestamp_seconds",
    "Unix timestamp of the last pipeline run that completed without raising.",
    ["kind"],
)

# The stall detector. A run that is swap-thrashing logs nothing and exits
# nothing — on 2026-07-13 one sat 13.5 h at ~20% CPU with a stale output file
# and a process that looked healthy. Liveness has to be asserted positively by
# the work itself, so every stage transition and every persisted activity page
# stamps this gauge. Staleness while a run is in flight *is* the signal.
pipeline_progress_timestamp_seconds = Gauge(
    "msos_pipeline_progress_timestamp_seconds",
    "Unix timestamp of the most recent unit of pipeline progress.",
)

pipeline_running = Gauge(
    "msos_pipeline_running",
    "1 while a pipeline run is in flight, 0 otherwise.",
)

pipeline_stage_rss_bytes = Gauge(
    "msos_pipeline_stage_rss_bytes",
    "Process resident set size sampled at the end of each pipeline stage.",
    ["stage"],
)

pipeline_records_written_total = Counter(
    "msos_pipeline_records_written_total",
    "Records persisted by the pipeline, by store.",
    ["store"],
)


# ── Upstream HTTP ────────────────────────────────────────────────────────────

upstream_requests_total = Counter(
    "msos_upstream_requests_total",
    "Upstream Polymarket/Goldsky requests by host, endpoint and outcome.",
    ["host", "endpoint", "outcome"],
)

upstream_request_duration_seconds = Histogram(
    "msos_upstream_request_duration_seconds",
    "Upstream request latency, measured around the transport call.",
    ["host", "endpoint"],
    buckets=_HTTP_BUCKETS,
)

# Measured around httpx's request *construction* only, which is where the
# cookie-jar walk lived. Splitting build from transport is what makes the
# 2026-07-10 failure mode legible: transport stayed fast the whole time.
upstream_request_build_seconds = Histogram(
    "msos_upstream_request_build_seconds",
    "Time spent constructing an upstream request, excluding transport.",
    ["host"],
    buckets=_BUILD_BUCKETS,
)

upstream_retries_total = Counter(
    "msos_upstream_retries_total",
    "Upstream request retries by host, endpoint and reason.",
    ["host", "endpoint", "reason"],
)

http_client_cookie_jar_size = Gauge(
    "msos_http_client_cookie_jar_size",
    "Cookie count on the shared httpx client after the most recent response.",
)

rate_limiter_wait_seconds = Histogram(
    "msos_rate_limiter_wait_seconds",
    "Time a worker blocked waiting for the shared upstream rate-limit budget.",
    buckets=_WAIT_BUCKETS,
)


# ── Enrichment / model health ────────────────────────────────────────────────
# These are the "is the science still working" series. Each corresponds to a
# quantity that went silently wrong for weeks while every process metric,
# every log line, and the whole test suite stayed green.

enrichment_duration_seconds = Histogram(
    "msos_enrichment_duration_seconds",
    "Duration of a full enrichment (two-pass empirical-Bayes) computation.",
    buckets=_STAGE_BUCKETS,
)

enrichment_wallets = Gauge(
    "msos_enrichment_wallets",
    "Wallets scored by the most recent enrichment pass.",
)

enrichment_resolved_bets = Gauge(
    "msos_enrichment_resolved_bets",
    "Resolved (settled) bets counted by the most recent enrichment pass.",
)

enrichment_markets = Gauge(
    "msos_enrichment_markets",
    "Markets available to the most recent enrichment pass.",
)

enrichment_tailable_wallets = Gauge(
    "msos_enrichment_tailable_wallets",
    "Wallets that cleared every tailability gate in the most recent pass.",
)

population_prior_mu = Gauge(
    "msos_population_prior_mu",
    "Fitted empirical-Bayes population prior mean, in log-odds.",
)

population_prior_sigma2 = Gauge(
    "msos_population_prior_sigma2",
    "Fitted empirical-Bayes population prior variance, in log-odds squared.",
)

skill_likelihood_saturated_ratio = Gauge(
    "msos_skill_likelihood_saturated_ratio",
    "Fraction of scored wallets whose skill_likelihood exceeds 0.999.",
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def process_rss_bytes() -> int | None:
    """Current resident set size, or None where it cannot be read cheaply.

    Reads ``/proc/self/statm`` on Linux (the deploy target) and falls back to
    ``resource.ru_maxrss`` elsewhere. Windows has neither, so local dev simply
    reports no RSS rather than paying for a psutil dependency.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * _PAGE_SIZE
    except (OSError, IndexError, ValueError):
        pass
    try:
        import resource  # optional, Unix-only

        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kibibytes, macOS reports bytes.
        return int(max_rss) * 1024
    except (ImportError, OSError, ValueError):
        return None


def heartbeat() -> None:
    """Stamp the progress gauge. Call from any loop that makes durable
    progress, so a stall is distinguishable from slow-but-alive work."""
    pipeline_progress_timestamp_seconds.set(time.time())


# Path segments that carry unbounded values (wallet addresses, condition ids,
# market ids). Collapsing them keeps the `endpoint` label's cardinality at the
# handful of real routes instead of one series per wallet.
def _normalize_segment(segment: str) -> str:
    if not segment:
        return segment
    if segment.startswith("0x") and len(segment) > 8:
        return ":id"
    if len(segment) > 24 and not segment.isalpha():
        return ":id"
    if segment.isdigit():
        return ":id"
    return segment


def split_endpoint(url: str) -> tuple[str, str]:
    """Split a URL into (host, normalized path) for use as metric labels."""
    parts = urlsplit(url)
    host = parts.netloc or "unknown"
    segments = [_normalize_segment(s) for s in parts.path.split("/")]
    path = "/".join(segments) or "/"
    return host, path


# ── Stage timing ─────────────────────────────────────────────────────────────
# The open stage is held per-thread rather than passed around, so the pipeline
# can announce a transition from anywhere it already emits progress without
# threading a timer object through call after call. Thread-local because deep
# runs hydrate wallets on a worker pool; a shared global would let one worker
# close another's stage.

_open_stage = threading.local()


def begin_stage(name: str) -> None:
    """Open a stage, closing whichever one was open before it.

    Re-entering the stage already open is a progress update ("wallet 37 of
    400"), not a transition: it counts as liveness but must not restart the
    timer.
    """
    if getattr(_open_stage, "name", None) == name:
        heartbeat()
        return
    end_stage()
    _open_stage.name = name
    _open_stage.started = time.perf_counter()
    heartbeat()


def end_stage() -> None:
    """Close the open stage, sampling RSS on the way out. Idempotent.

    RSS is sampled here rather than continuously: the failure this guards
    against (unbounded accumulation across a stage) is visible in the
    end-of-stage reading, and sampling costs a file read instead of a thread.
    """
    name = getattr(_open_stage, "name", None)
    if name is None:
        return
    pipeline_stage_duration_seconds.labels(stage=name).observe(
        time.perf_counter() - _open_stage.started
    )
    rss = process_rss_bytes()
    if rss is not None:
        pipeline_stage_rss_bytes.labels(stage=name).set(rss)
    _open_stage.name = None
    heartbeat()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Scoped form of begin_stage/end_stage for a self-contained block."""
    begin_stage(name)
    try:
        yield
    finally:
        end_stage()


class track_run:
    """Context manager marking a pipeline run in flight.

    Sets the running gauge, stamps progress on entry, and records the terminal
    outcome. `outcome="failure"` covers any exception, including the
    cancellation a scheduler shutdown raises — a run that did not finish did
    not produce data, and the alert should treat it as such.

    Closing the open stage on exit is what makes the *last* stage of a run get
    timed at all. Stage timings are derived from transitions, and the final
    stage has no successor to close it — without this the most expensive stage
    of every run (enrichment) would silently never be observed.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def __enter__(self) -> Self:
        pipeline_running.set(1)
        heartbeat()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        end_stage()
        pipeline_running.set(0)
        outcome = "success" if exc_type is None else "failure"
        pipeline_runs_total.labels(kind=self._kind, outcome=outcome).inc()
        if exc_type is None:
            pipeline_last_success_timestamp_seconds.labels(kind=self._kind).set(
                time.time()
            )
        heartbeat()
