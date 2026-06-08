"""Thread-safe per-host rate limiter for Polymarket HTTP clients."""
from __future__ import annotations

import threading
import time


class HostRateLimiter:
    """Simple token-bucket spacing: at most one request per (1/rps) seconds."""

    def __init__(self, *, rps: float = 5.0) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval
