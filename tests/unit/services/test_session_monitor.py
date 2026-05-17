"""Unit tests for SessionMonitor.check().

Layer 4 — services (per docs/architecture/09-test-strategy.md §Layer 4):
  tested with fakes for infra collaborators (FakeHttpClient, FakeEventBus,
  FakeClock), no real I/O.

Invariants covered:
  - EXPIRED on login redirect (final_url contains /login)
  - ACTIVE on HTTP 200 without redirect
  - EXPIRED (fail-safe) on unexpected HTTP status
  - OSError propagates; EventBus untouched
  - Fake completeness: FakeHttpClient.get() and FakeEventBus.publish() are
    exercised (ADR-041 §Fake canon).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import HttpResponse, SessionStatus, SseSessionExpired
from fis_monitor.services.session_monitor import SessionMonitor
from tests.fakes.clock import FakeClock
from tests.fakes.event_bus import FakeEventBus
from tests.fakes.http_client import FakeHttpClient

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_BASE_URL = "https://example.test"
_FIXED_NOW = datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)


def _make_monitor(
    *,
    http_client: FakeHttpClient,
    event_bus: FakeEventBus,
) -> SessionMonitor:
    return SessionMonitor(
        http_client=http_client,
        event_bus=event_bus,
        clock=FakeClock(now=_FIXED_NOW),
        base_url=_BASE_URL,
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_check_returns_expired_and_publishes_event_on_login_redirect() -> None:
    """EXPIRED path: final_url contains /login → EXPIRED + SseSessionExpired published.

    Exercises FakeHttpClient.get() and FakeEventBus.publish() (ADR-041 fake canon).
    """
    http = FakeHttpClient([
        HttpResponse(
            status=200,
            final_url="https://example.test/login?return=%2Fcabinet%2F",
            text="",
            headers={},
        )
    ])
    bus = FakeEventBus()
    monitor = _make_monitor(http_client=http, event_bus=bus)

    result = monitor.check()

    assert result is SessionStatus.EXPIRED
    assert len(bus.published) == 1
    evt = bus.published[0]
    assert isinstance(evt, SseSessionExpired)
    assert evt.timestamp == _FIXED_NOW
    # Verify exact probe URL was used
    assert http.calls == [_BASE_URL + "/cabinet/"]


def test_check_returns_active_on_200_no_redirect() -> None:
    """ACTIVE path: HTTP 200 with no /login in final_url → ACTIVE, no event published."""
    http = FakeHttpClient([
        HttpResponse(
            status=200,
            final_url="https://example.test/cabinet/",
            text="<html>cabinet</html>",
            headers={},
        )
    ])
    bus = FakeEventBus()
    monitor = _make_monitor(http_client=http, event_bus=bus)

    result = monitor.check()

    assert result is SessionStatus.ACTIVE
    assert bus.published == []


def test_check_returns_expired_on_unexpected_status() -> None:
    """Fail-safe: unexpected HTTP status (e.g. 500) → EXPIRED + event published."""
    http = FakeHttpClient([
        HttpResponse(
            status=500,
            final_url="https://example.test/cabinet/",
            text="Internal Server Error",
            headers={},
        )
    ])
    bus = FakeEventBus()
    monitor = _make_monitor(http_client=http, event_bus=bus)

    result = monitor.check()

    assert result is SessionStatus.EXPIRED
    assert len(bus.published) == 1
    assert isinstance(bus.published[0], SseSessionExpired)


def test_check_propagates_network_error_and_does_not_publish() -> None:
    """Network errors (OSError) propagate to caller; EventBus remains untouched."""
    http = FakeHttpClient()
    http.enqueue_error(OSError("network unreachable"))
    bus = FakeEventBus()
    monitor = _make_monitor(http_client=http, event_bus=bus)

    with pytest.raises(OSError, match="network unreachable"):
        monitor.check()

    assert bus.published == []
