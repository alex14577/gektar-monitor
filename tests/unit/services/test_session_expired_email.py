"""Unit tests for SessionExpiredEmailService.

Layer 4 (services) unit tests — fake EmailNotifier + fake StateRepository.
Per test-strategy §Layer 2 (Application services): pure mocks, no DB, no SMTP.

Invariants verified:
  1. SseSessionExpired event → exactly ONE send per configured recipient.
  2. Second SseSessionExpired in same epoch → idempotent (no duplicate send).
  3. After on_login_or_refresh_success() → flag cleared → new expiry sends email again.
  4. email.enabled=False → no send, guard NOT set (UI modal unaffected).
  5. DnD active → no send, guard NOT set (so post-DnD event can still send).
  6. No recipients configured → send not called, but guard IS set (prevent log-spam).
  7. SMTP failure → guard IS set (fire-and-forget; no retry spam).
  8. Anti-fake: all FakeSmtpEmailNotifier + FakeStateRepository methods exercised.
"""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from fis_monitor.domain.models import NotifyResult, Settings, SseSessionExpired
from fis_monitor.services.session_expired_email import (
    SESSION_EXPIRED_EMAIL_SENT_KEY,
    SessionExpiredEmailService,
)

# ---------------------------------------------------------------------------
# Canonical test timestamp
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSmtpEmailNotifier:
    """Fake for SmtpEmailNotifier — records calls to send_session_expired."""

    def __init__(self, *, result: NotifyResult | None = None) -> None:
        self._result = result or NotifyResult(ok=True, detail="sent", retryable=False)
        self.calls: list[str] = []  # recipient per call

    def send_session_expired(self, recipient: str) -> NotifyResult:
        self.calls.append(recipient)
        return self._result


class FakeStateRepository:
    """In-memory KV store satisfying the StateRepository Protocol."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class FakeClock:
    """Injected clock that returns a fixed datetime."""

    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeConfigSource:
    """ConfigSource stub that returns a mutable Settings."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> Any:  # pragma: no cover
        return MagicMock()

    def save(self, settings: Settings) -> None:  # pragma: no cover
        self._settings = settings


def _make_settings(
    *,
    email_enabled: bool = True,
    recipients: list[str] | None = None,
    dnd_until: str | None = None,
) -> Settings:
    """Build a Settings with controllable email and DnD state."""
    from fis_monitor.domain.models import DndConfig, EmailConfig, NotificationsConfig
    email_cfg = EmailConfig(enabled=email_enabled, recipients=recipients or [])
    dnd_cfg = DndConfig(until=dnd_until)
    notif_cfg = NotificationsConfig(email=email_cfg, dnd=dnd_cfg)
    return Settings(notifications=notif_cfg)


class FakeDndService:
    """Fake DndService — controllable is_active()."""

    def __init__(self, *, active: bool = False) -> None:
        self._active = active

    def is_active(self, now: datetime) -> bool:
        return self._active


class _FakeSubscription:
    """Context-manager subscription returned by FakeEventBus.subscribe().

    ``wait_one`` blocks up to *timeout* seconds using a real ``queue.Queue``
    so tests can inject events after the consumer loop is running without
    hitting the race where events are injected before ``subscribe()`` is called.
    """

    def __init__(self, bus: FakeEventBus) -> None:
        self._bus = bus
        self._queue: queue.Queue[Any] = queue.Queue()
        self.alive = True

    def __enter__(self) -> _FakeSubscription:
        return self

    def __exit__(self, *args: Any) -> None:
        self.unsubscribe()

    def unsubscribe(self) -> None:
        if self in self._bus._subscriptions:
            self._bus._subscriptions.remove(self)
        self.alive = False

    def wait_one(self, timeout: float) -> Any | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def iter(self) -> Any:  # pragma: no cover
        try:
            while True:
                yield self._queue.get_nowait()
        except queue.Empty:
            return


class FakeEventBus:
    """EventBus stub that delivers events injected via inject_event().

    Events injected via ``inject_event()`` are pushed only to *already-active*
    subscriptions.  This mirrors the production ``ThreadEventBus`` contract:
    events published before ``subscribe()`` is called are not replayed.

    To avoid the race where tests inject events before ``consumer_loop``
    calls ``subscribe()``, use ``_run_service_for_events`` (which injects
    events from inside a thread after the subscription is live) or call
    ``svc._handle(event)`` directly for synchronous unit tests.
    """

    def __init__(self) -> None:
        self._subscriptions: list[_FakeSubscription] = []
        # ``_subscribed`` is set by the first subscribe() call so that
        # _run_service_for_events can wait until the loop is ready.
        self._subscribed = threading.Event()

    def inject_event(self, event: Any) -> None:
        """Push an event into all active subscriptions."""
        for sub in list(self._subscriptions):
            sub._queue.put(event)

    def publish(self, event: Any) -> None:  # pragma: no cover
        self.inject_event(event)

    def subscribe(self) -> _FakeSubscription:
        sub = _FakeSubscription(bus=self)
        self._subscriptions.append(sub)
        self._subscribed.set()
        return sub


# ---------------------------------------------------------------------------
# Helper: run consumer_loop for N events then stop
# ---------------------------------------------------------------------------

def _run_service_for_events(
    svc: SessionExpiredEmailService,
    events: list[Any],
    bus: FakeEventBus,
) -> None:
    """Run consumer_loop in a thread, inject events once subscribed, then stop.

    Strategy:
    1. Start the consumer_loop in a background thread — it calls bus.subscribe()
       which sets bus._subscribed.
    2. Wait until bus._subscribed is set (subscription is live).
    3. Inject all events into the live subscription.
    4. Wait until all subscription queues are drained (events processed).
    5. Signal stop and join the thread.

    This avoids the race where events are injected before subscribe() is called.
    """
    import time

    def _run() -> None:
        svc.consumer_loop()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Step 2: wait for subscription to be live (set by bus.subscribe())
    subscribed = bus._subscribed.wait(timeout=2.0)
    assert subscribed, "consumer_loop did not call subscribe() within 2s"

    # Step 3: inject events into the live subscription
    for ev in events:
        bus.inject_event(ev)

    # Step 4: wait until all subscription queues are drained
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        all_empty = all(sub._queue.empty() for sub in bus._subscriptions)
        if all_empty:
            break
        time.sleep(0.01)

    # Step 5: signal stop and join
    svc.stop_event.set()
    t.join(timeout=2.0)


def _make_svc(
    *,
    notifier: FakeSmtpEmailNotifier | None = None,
    state_repo: FakeStateRepository | None = None,
    config: Settings | None = None,
    dnd_active: bool = False,
    recipients: list[str] | None = None,
    email_enabled: bool = True,
) -> tuple[SessionExpiredEmailService, FakeSmtpEmailNotifier, FakeStateRepository, FakeEventBus]:
    notifier = notifier or FakeSmtpEmailNotifier()
    state_repo = state_repo or FakeStateRepository()
    if config is None:
        config = _make_settings(
            email_enabled=email_enabled,
            recipients=recipients if recipients is not None else ["admin@example.com"],
        )
    bus = FakeEventBus()
    svc = SessionExpiredEmailService(
        email_notifier=notifier,  # type: ignore[arg-type]
        state_repo=state_repo,
        config_source=FakeConfigSource(config),
        event_bus=bus,
        clock=FakeClock(),
        dnd_service=FakeDndService(active=dnd_active),
        stop_event=threading.Event(),
    )
    return svc, notifier, state_repo, bus


# ---------------------------------------------------------------------------
# Anti-fake tests (per invariant #8 / memory orchestrator-playbook)
# ---------------------------------------------------------------------------


def test_fake_smtp_notifier_all_methods() -> None:
    """FakeSmtpEmailNotifier.send_session_expired() is callable and returns NotifyResult."""
    n = FakeSmtpEmailNotifier()
    result = n.send_session_expired("user@example.com")
    assert result.ok
    assert n.calls == ["user@example.com"]


def test_fake_state_repository_all_methods() -> None:
    """FakeStateRepository get/set/delete all exercised."""
    repo = FakeStateRepository()
    assert repo.get("k") is None
    repo.set("k", "v")
    assert repo.get("k") == "v"
    repo.delete("k")
    assert repo.get("k") is None
    # delete non-existent is a no-op
    repo.delete("k")


# ---------------------------------------------------------------------------
# Core invariants
# ---------------------------------------------------------------------------


def test_session_expired_event_sends_exactly_one_email() -> None:
    """SseSessionExpired → exactly one send per recipient (invariant #1)."""
    svc, notifier, state_repo, bus = _make_svc(recipients=["admin@example.com"])
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert notifier.calls == ["admin@example.com"]
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) == "1"


def test_session_expired_multiple_recipients() -> None:
    """SseSessionExpired → one send per recipient (both configured)."""
    svc, notifier, state_repo, bus = _make_svc(
        recipients=["a@example.com", "b@example.com"]
    )
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert sorted(notifier.calls) == ["a@example.com", "b@example.com"]
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) == "1"


def test_second_session_expired_event_is_idempotent() -> None:
    """Second SseSessionExpired in same epoch → no duplicate send (invariant #2).

    Uses ``_handle`` directly to test handler logic without the consumer loop —
    the loop is an I/O boundary, handler idempotency is the domain invariant.
    """
    svc, notifier, _state_repo, _bus = _make_svc(recipients=["admin@example.com"])
    event = SseSessionExpired(timestamp=_NOW)

    # First expiry
    svc._handle(event)
    assert len(notifier.calls) == 1

    # Second expiry in same epoch: guard already set → no duplicate send
    svc._handle(SseSessionExpired(timestamp=_NOW))
    assert len(notifier.calls) == 1  # still only one call


def test_after_login_success_flag_cleared_new_expiry_sends_email() -> None:
    """After on_login_or_refresh_success() → flag reset → new expiry sends email (invariant #3).

    Uses ``_handle`` directly to test the reset-then-resend sequence without
    the consumer loop (loop is I/O boundary, not the business invariant under test).
    """
    svc, notifier, state_repo, _bus = _make_svc(recipients=["admin@example.com"])
    event = SseSessionExpired(timestamp=_NOW)

    # First epoch: expires → send
    svc._handle(event)
    assert len(notifier.calls) == 1

    # Simulate successful login → reset flag
    svc.on_login_or_refresh_success()
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) is None

    # New epoch: expires → send again
    svc._handle(SseSessionExpired(timestamp=_NOW))
    assert len(notifier.calls) == 2


def test_email_disabled_no_send_guard_not_set() -> None:
    """email.enabled=False → no send, guard NOT set (invariant #4)."""
    svc, notifier, state_repo, bus = _make_svc(email_enabled=False)
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert notifier.calls == []
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) is None


def test_dnd_active_no_send_guard_not_set() -> None:
    """DnD active → no send, guard NOT set so post-DnD event can still send (invariant #5)."""
    svc, notifier, state_repo, bus = _make_svc(dnd_active=True)
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert notifier.calls == []
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) is None


def test_no_recipients_no_send_guard_set() -> None:
    """No recipients → no send, but guard IS set to prevent log-spam (invariant #6)."""
    svc, notifier, state_repo, bus = _make_svc(recipients=[])
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert notifier.calls == []
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) == "1"


def test_smtp_failure_guard_still_set() -> None:
    """SMTP failure → guard IS set (fire-and-forget, no retry spam) (invariant #7)."""
    failing_notifier = FakeSmtpEmailNotifier(
        result=NotifyResult(ok=False, detail="connect_error", retryable=True)
    )
    svc, _notifier, state_repo, bus = _make_svc(
        notifier=failing_notifier, recipients=["admin@example.com"]
    )
    event = SseSessionExpired(timestamp=_NOW)

    _run_service_for_events(svc, [event], bus)

    assert len(failing_notifier.calls) == 1  # attempted
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) == "1"


def test_non_session_expired_events_ignored() -> None:
    """Other SSE events are not handled — no call, no state change."""
    from fis_monitor.domain.models import SseCycleError

    svc, notifier, state_repo, bus = _make_svc(recipients=["admin@example.com"])
    other_event = SseCycleError(
        timestamp=_NOW, cycle_id=1, error_category="network"
    )

    _run_service_for_events(svc, [other_event], bus)

    assert notifier.calls == []
    assert state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) is None
