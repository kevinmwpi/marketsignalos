"""Thread-safe per-host rate limiter for Polymarket HTTP clients."""
from __future__ import annotations

import threading
import time

from . import metrics


class HostRateLimiter:
    """Simple token-bucket spacing: at most one request per (1/rps) seconds."""

    def __init__(self, *, rps: float = 5.0) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        # Observed even when the wait is zero: the shape of this histogram is
        # how you tell "upstream is slow" from "we are queued behind our own
        # shared budget", and only the first of those gets better by adding
        # workers. Concurrency tuning without it is guesswork.
        waited = 0.0
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                waited = self._next_at - now
                time.sleep(waited)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval
        metrics.rate_limiter_wait_seconds.observe(waited)
