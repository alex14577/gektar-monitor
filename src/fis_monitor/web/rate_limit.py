"""In-memory token-bucket rate limiter.

Implements a simple fixed-window (rolling) rate limiter keyed by an arbitrary
string (typically a client IP address).  The limiter is intentionally thin and
stateless between requests — no persistence, no distributed coordination.

Usage::

    limiter = RateLimiter(max_requests=1, window_seconds=60)
    if not limiter.acquire("127.0.0.1", now=time.monotonic()):
        raise HTTPException(status_code=429, detail="Too many requests")

Thread-safety: ``acquire()`` uses a ``threading.Lock`` to protect the internal
window map, making it safe for concurrent ASGI workers.

Design notes:
  - Sliding window: each key stores the timestamp of the *oldest* request in
    the current window. The window slides forward when the oldest entry is
    older than ``window_seconds``.
  - Memory: entries are evicted lazily on ``acquire()`` calls. For a small
    number of distinct IPs (loopback-only deployment) this is fine.
  - ``now`` is injected for deterministic testing — production callers pass
    ``time.monotonic()``.
"""

from __future__ import annotations

import threading

__all__ = ["RateLimiter"]


class RateLimiter:
    """Fixed-window in-memory token-bucket rate limiter.

    Args:
        max_requests: Maximum number of requests allowed per window.
        window_seconds: Duration of each window in seconds.
    """

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        # key → list of request timestamps (monotonic) within the current window
        self._windows: dict[str, list[float]] = {}

    def acquire(self, key: str, *, now: float) -> bool:
        """Attempt to consume one token for ``key``.

        Args:
            key:  Rate-limit key (e.g. client IP string).
            now:  Current monotonic time in seconds (pass ``time.monotonic()``).

        Returns:
            ``True`` if the request is within the allowed rate, ``False`` if
            the rate limit is exceeded.
        """
        cutoff = now - self._window
        with self._lock:
            timestamps = self._windows.get(key, [])
            # Evict entries older than the window.
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self._max:
                # Rate limit exceeded — do NOT record this attempt.
                self._windows[key] = timestamps
                return False
            timestamps.append(now)
            self._windows[key] = timestamps
            return True
