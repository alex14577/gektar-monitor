"""SystemClock — production implementation of the Clock Protocol.

Stateless, singleton-friendly.  Injected into every service that needs
deterministic time in tests (tests pass a ``FakeClock`` instead).

See: domain/interfaces.py::Clock, docs/architecture/07-concurrency.md §7.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """Production wall-clock backed by the OS.

    Implements the ``Clock`` Protocol (domain/interfaces.py:158-175).
    Stateless — no constructor arguments required.  Can be used as a
    module-level singleton (``SYSTEM_CLOCK = SystemClock()``).
    """

    def now(self) -> datetime:
        """Return current aware datetime in UTC (Python 3.12+ style)."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return a monotonically-increasing float (seconds)."""
        return time.monotonic()
