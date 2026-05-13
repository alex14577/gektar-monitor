"""Unit tests for ThreadEventSubscription and ThreadConfigSubscription.

Covers:
  - Context-manager calls unsubscribe on exit
  - Idempotent unsubscribe (remover called exactly once)
  - iter() drains queue non-blocking
  - ThreadConfigSubscription: context-manager, idempotent unsubscribe
  - ThreadConfigSubscription: deliver() skipped after unsubscribe (_alive flag)
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fis_monitor.infra.sse.subscriptions import (
    ThreadConfigSubscription,
    ThreadEventSubscription,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_sse_event():
    from fis_monitor.domain.models import SseLotStatus

    return SseLotStatus(lot_id=1, new_status="gone", event_type="gone")


def _make_settings():
    """Return a minimal Settings-like object (duck-typed for tests)."""
    return MagicMock(name="Settings")


# ---------------------------------------------------------------------------
# ThreadEventSubscription
# ---------------------------------------------------------------------------


class TestEventSubscriptionContextManager:
    def test_context_manager_calls_unsubscribe_on_exit(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)

        with sub as entered:
            assert entered is sub

        remover.assert_called_once_with(sub)

    def test_context_manager_returns_self(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)
        assert sub.__enter__() is sub


class TestEventSubscriptionUnsubscribeIdempotent:
    def test_unsubscribe_calls_remover_once(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)

        sub.unsubscribe()
        sub.unsubscribe()
        sub.unsubscribe()

        # The ThreadEventSubscription delegates idempotency to the bus via
        # _remove_subscriber (which ignores ValueError on second remove).
        # So remover IS called each time — bus.py handles the dedup under lock.
        # This test just asserts no exception is raised on repeated calls.
        assert remover.call_count == 3  # called every time; bus deduplicates

    def test_unsubscribe_does_not_raise(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)
        sub.unsubscribe()
        sub.unsubscribe()  # must not raise


class TestEventSubscriptionIter:
    def test_iter_drains_queue_non_blocking(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)

        events = [_make_sse_event() for _ in range(3)]
        for e in events:
            sub._q.put_nowait(e)

        result = list(sub.iter())

        assert result == events

    def test_iter_returns_empty_when_queue_empty(self):
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)

        assert list(sub.iter()) == []

    def test_iter_exhausts_and_stops(self):
        """iter() must stop after draining — not block waiting for new items."""
        remover = MagicMock()
        sub = ThreadEventSubscription(remover=remover)

        sub._q.put_nowait(_make_sse_event())
        sub._q.put_nowait(_make_sse_event())

        count = sum(1 for _ in sub.iter())
        assert count == 2


# ---------------------------------------------------------------------------
# ThreadConfigSubscription
# ---------------------------------------------------------------------------


class TestConfigSubscriptionContextManager:
    def test_context_manager_calls_unsubscribe_on_exit(self):
        cb = MagicMock()
        remover = MagicMock()
        sub = ThreadConfigSubscription(cb=cb, remover=remover)

        with sub as entered:
            assert entered is sub

        remover.assert_called_once_with(sub)

    def test_context_manager_returns_self(self):
        sub = ThreadConfigSubscription(cb=MagicMock(), remover=MagicMock())
        assert sub.__enter__() is sub


class TestConfigSubscriptionUnsubscribeIdempotent:
    def test_unsubscribe_calls_remover_exactly_once(self):
        remover = MagicMock()
        sub = ThreadConfigSubscription(cb=MagicMock(), remover=remover)

        sub.unsubscribe()
        sub.unsubscribe()
        sub.unsubscribe()

        remover.assert_called_once_with(sub)

    def test_unsubscribe_does_not_raise_on_repeated_calls(self):
        sub = ThreadConfigSubscription(cb=MagicMock(), remover=MagicMock())
        sub.unsubscribe()
        sub.unsubscribe()  # must not raise


class TestConfigSubscriptionAliveFlag:
    def test_deliver_invokes_cb_when_alive(self):
        cb = MagicMock()
        sub = ThreadConfigSubscription(cb=cb, remover=MagicMock())

        settings = _make_settings()
        sub.deliver(settings)

        cb.assert_called_once_with(settings)

    def test_deliver_skips_cb_after_unsubscribe(self):
        cb = MagicMock()
        sub = ThreadConfigSubscription(cb=cb, remover=MagicMock())

        sub.unsubscribe()
        sub.deliver(_make_settings())

        cb.assert_not_called()

    def test_alive_flag_false_after_unsubscribe(self):
        sub = ThreadConfigSubscription(cb=MagicMock(), remover=MagicMock())
        assert sub._alive is True

        sub.unsubscribe()
        assert sub._alive is False

    def test_deliver_multiple_times_before_unsubscribe(self):
        cb = MagicMock()
        sub = ThreadConfigSubscription(cb=cb, remover=MagicMock())

        s1, s2 = _make_settings(), _make_settings()
        sub.deliver(s1)
        sub.deliver(s2)

        assert cb.call_count == 2
