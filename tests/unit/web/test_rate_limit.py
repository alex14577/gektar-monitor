"""Unit tests for RateLimiter.

Coverage:
  1. acquire under limit → True.
  2. acquire at limit → True (last allowed).
  3. acquire over limit within window → False.
  4. acquire after window expires → True (window slides).
  5. different keys are independent.
  6. max_requests=1, window=60s: first ok, second denied, after 60s ok again.
  7. Constructor validation: max_requests < 1 raises ValueError.
  8. Constructor validation: window_seconds <= 0 raises ValueError.
"""

from __future__ import annotations

import pytest

from fis_monitor.web.rate_limit import RateLimiter


def test_acquire_under_limit() -> None:
    """Single request under the limit → True."""
    rl = RateLimiter(max_requests=3, window_seconds=60.0)
    assert rl.acquire("ip1", now=0.0) is True


def test_acquire_at_limit() -> None:
    """Exactly max_requests requests within window — last one True."""
    rl = RateLimiter(max_requests=3, window_seconds=60.0)
    assert rl.acquire("ip1", now=0.0) is True
    assert rl.acquire("ip1", now=1.0) is True
    assert rl.acquire("ip1", now=2.0) is True


def test_acquire_over_limit_denied() -> None:
    """Request exceeding max_requests within window → False."""
    rl = RateLimiter(max_requests=3, window_seconds=60.0)
    rl.acquire("ip1", now=0.0)
    rl.acquire("ip1", now=1.0)
    rl.acquire("ip1", now=2.0)
    assert rl.acquire("ip1", now=3.0) is False


def test_acquire_after_window_expires() -> None:
    """Request after the window expires → True (old entries evicted)."""
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    assert rl.acquire("ip1", now=0.0) is True
    assert rl.acquire("ip1", now=30.0) is False  # still within window
    assert rl.acquire("ip1", now=61.0) is True   # first entry is now older than 60s


def test_different_keys_independent() -> None:
    """Rate limit is per-key — different IPs are independent."""
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    assert rl.acquire("ip1", now=0.0) is True
    assert rl.acquire("ip2", now=0.0) is True
    assert rl.acquire("ip1", now=1.0) is False
    assert rl.acquire("ip2", now=1.0) is False


def test_rate_limit_1_per_60s() -> None:
    """Integration: 1 req / 60 s — second request denied, after 60s allowed."""
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    now = 0.0
    assert rl.acquire("client", now=now) is True
    assert rl.acquire("client", now=now + 59.9) is False
    assert rl.acquire("client", now=now + 60.1) is True


def test_constructor_invalid_max_requests() -> None:
    """max_requests < 1 → ValueError."""
    with pytest.raises(ValueError, match="max_requests"):
        RateLimiter(max_requests=0, window_seconds=60.0)


def test_constructor_invalid_window() -> None:
    """window_seconds <= 0 → ValueError."""
    with pytest.raises(ValueError, match="window_seconds"):
        RateLimiter(max_requests=1, window_seconds=0.0)


def test_denied_request_not_recorded() -> None:
    """A denied request (over limit) must not count toward future requests."""
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    rl.acquire("ip1", now=0.0)   # allowed
    rl.acquire("ip1", now=1.0)   # denied (not recorded)
    # After 60s from the first (allowed) request, next should succeed.
    assert rl.acquire("ip1", now=61.0) is True
