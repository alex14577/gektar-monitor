"""Unit tests for NotifierDispatcher.

Coverage:
- dispatch(): fire-forget enqueue, queue-full drop + warning
- _send_one(): reserve → mark_attempt → send → mark_sent (happy path)
- _send_one(): skip if already sent/permanent_fail
- _send_one(): mark_attempt returns None (R4-C4 race)
- _send_one(): attempt_no > MAX_TOTAL_ATTEMPTS (R4-M6 cap)
- _send_one(): retryable failures with backoff + eventual success
- _send_one(): non-retryable failure → permanent_fail + event published
- _send_one(): all attempts exhausted → pending (no permanent_fail)
- _send_one(): stop_event during backoff → immediate return
- consumer_loop(): drains queue until stop_event
- consumer_loop(): recovery picks up stale / zombie pending rows (R4-C3)
- consumer_loop(): recovery skip unknown channel
- consumer_loop(): recovery lot missing → permanent_fail
- _dispatch_all_channels(): all notifiers x recipients
- _recipients_of(): email from config_source, browser → ['local'], unknown → []
- _publish_smtp_failed(): PII-safe detail cap
- Anti-mock: all fake method surfaces exercised

PII contract: recipient addresses never appear in logs plaintext.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

from fis_monitor.domain.interfaces import (
    EventSubscription,
)
from fis_monitor.domain.models import (
    EmailConfig,
    FiltersConfig,
    LotPublicDTO,
    LotUpsertResult,
    NotificationRecord,
    NotificationsConfig,
    NotifierConfig,
    NotifyResult,
    Settings,
    SseEvent,
    SseSmtpFailed,
)
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.services.filter_matcher import RfSubjectFilterMatcher
from fis_monitor.services.notifier_dispatcher import (
    MAX_TOTAL_ATTEMPTS,
    NotifierDispatcher,
    RfSubjectFilteredEmailNotifier,
    SubscribedAtFilteredNotifier,
)
from tests.factories import make_lot, make_settings

# ---------------------------------------------------------------------------
# UTC instant used across tests
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
_MINUTE_AGO = datetime(2026, 5, 13, 11, 59, 0, tzinfo=UTC)


# ===========================================================================
# Fake domain objects
# ===========================================================================


class FakeClock:
    """Deterministic clock that returns a fixed instant."""

    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class _FakeEventSubscription:
    def __enter__(self) -> _FakeEventSubscription:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def unsubscribe(self) -> None:
        pass

    def iter(self):  # type: ignore[override]
        return iter([])


class FakeEventBus:
    """Records published events for assertion."""

    def __init__(self) -> None:
        self.published: list[SseEvent] = []
        self._lock = threading.Lock()

    def publish(self, event: SseEvent) -> None:
        with self._lock:
            self.published.append(event)

    def subscribe(self) -> EventSubscription[SseEvent]:
        return _FakeEventSubscription()  # type: ignore[return-value]

    def smtp_failed_events(self) -> list[SseSmtpFailed]:
        with self._lock:
            return [e for e in self.published if isinstance(e, SseSmtpFailed)]


class _FakeConfigSubscription:
    def __enter__(self) -> _FakeConfigSubscription:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def unsubscribe(self) -> None:
        pass


class FakeConfigSource:
    """Returns a fixed Settings snapshot."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or make_settings()

    def current(self) -> Settings:
        return self._settings

    def subscribe(self, cb: Any) -> _FakeConfigSubscription:
        return _FakeConfigSubscription()


# ---------------------------------------------------------------------------
# FakeNotificationsRepository
# ---------------------------------------------------------------------------

_StatusLiteral = Literal["pending", "sent", "permanent_fail"]


class FakeNotificationsRepository:
    """In-memory notifications state machine.

    PK: (lot_id, channel, recipient) → row dict.
    """

    def __init__(self) -> None:
        # rows: (lot_id, channel, recipient) → dict with status, attempt_no,
        # last_attempt_at, sent_at
        self._rows: dict[tuple[int, str, str], dict[str, Any]] = {}
        self._calls: list[str] = []  # track method invocations

    # --- R4-C4: optional override to return None on mark_attempt
    _force_mark_attempt_none: bool = False

    def _key(self, lot_id: int, channel: str, recipient: str) -> tuple[int, str, str]:
        return (lot_id, channel, recipient)

    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
        self._calls.append("reserve")
        key = self._key(lot_id, channel, recipient)
        if key in self._rows:
            return False
        self._rows[key] = {
            "lot_id": lot_id,
            "channel": channel,
            "recipient": recipient,
            "status": "pending",
            "attempt_no": 0,
            "last_attempt_at": None,
            "sent_at": None,
        }
        return True

    def status_of(self, lot_id: int, channel: str, recipient: str) -> _StatusLiteral | None:
        self._calls.append("status_of")
        row = self._rows.get(self._key(lot_id, channel, recipient))
        if row is None:
            return None
        return row["status"]  # type: ignore[return-value]

    def mark_attempt(self, lot_id: int, channel: str, recipient: str, at: datetime) -> int | None:
        self._calls.append("mark_attempt")
        if self._force_mark_attempt_none:
            return None
        key = self._key(lot_id, channel, recipient)
        row = self._rows.get(key)
        if row is None:
            return None
        if row["status"] in ("sent", "permanent_fail"):
            return None  # R4-C4
        row["attempt_no"] += 1
        row["last_attempt_at"] = at
        return row["attempt_no"]  # type: ignore[return-value]

    def mark_sent(self, lot_id: int, channel: str, recipient: str, at: datetime) -> None:
        self._calls.append("mark_sent")
        key = self._key(lot_id, channel, recipient)
        if key in self._rows:
            self._rows[key]["status"] = "sent"
            self._rows[key]["sent_at"] = at

    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None:
        self._calls.append("mark_permanent_fail")
        key = self._key(lot_id, channel, recipient)
        if key in self._rows:
            self._rows[key]["status"] = "permanent_fail"
        else:
            # Row may not exist yet if called without reserve (e.g. recovery lot-missing)
            self._rows[key] = {
                "lot_id": lot_id,
                "channel": channel,
                "recipient": recipient,
                "status": "permanent_fail",
                "attempt_no": 0,
                "last_attempt_at": None,
                "sent_at": None,
            }

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
        self._calls.append("list_pending_older_than")
        cutoff = _NOW - age
        result = []
        for row in self._rows.values():
            if row["status"] != "pending":
                continue
            lat = row["last_attempt_at"]
            # Include zombie rows (last_attempt_at IS NULL, R4-C3) and stale rows
            if lat is None or lat < cutoff:
                result.append(
                    NotificationRecord(
                        lot_id=row["lot_id"],
                        channel=row["channel"],
                        recipient=row["recipient"],
                        status="pending",
                        attempt_no=row["attempt_no"],
                        last_attempt_at=lat,
                        sent_at=row["sent_at"],
                    )
                )
        return result

    def list_recent(self, limit: int) -> list[NotificationRecord]:
        self._calls.append("list_recent")
        rows = list(self._rows.values())[-limit:]
        return [
            NotificationRecord(
                lot_id=r["lot_id"],
                channel=r["channel"],
                recipient=r["recipient"],
                status=r["status"],
                attempt_no=r["attempt_no"],
                last_attempt_at=r["last_attempt_at"],
                sent_at=r["sent_at"],
            )
            for r in rows
        ]

    # --- helpers for assertions
    def get_status(self, lot_id: int, channel: str, recipient: str) -> str | None:
        key = self._key(lot_id, channel, recipient)
        row = self._rows.get(key)
        return row["status"] if row else None

    def get_attempt_no(self, lot_id: int, channel: str, recipient: str) -> int:
        key = self._key(lot_id, channel, recipient)
        return self._rows[key]["attempt_no"]

    def seed_pending(
        self,
        lot_id: int,
        channel: str,
        recipient: str,
        *,
        attempt_no: int = 0,
        last_attempt_at: datetime | None = None,
    ) -> None:
        """Directly insert a pending row for test setup."""
        key = self._key(lot_id, channel, recipient)
        self._rows[key] = {
            "lot_id": lot_id,
            "channel": channel,
            "recipient": recipient,
            "status": "pending",
            "attempt_no": attempt_no,
            "last_attempt_at": last_attempt_at,
            "sent_at": None,
        }


# ---------------------------------------------------------------------------
# FakeLotRepository
# ---------------------------------------------------------------------------


class FakeRegionSubscriptionRepository:
    """In-memory RegionSubscriptionRepository for tests."""

    def __init__(self) -> None:
        self._rows: dict[int, datetime] = {}
        self._calls: list[str] = []

    def get_subscribed_at(self, region_id: int) -> datetime | None:
        self._calls.append(f"get_subscribed_at:{region_id}")
        return self._rows.get(region_id)

    def set_if_absent(self, region_id: int, subscribed_at: datetime) -> bool:
        self._calls.append(f"set_if_absent:{region_id}")
        if region_id in self._rows:
            return False
        self._rows[region_id] = subscribed_at
        return True

    def delete(self, region_id: int) -> None:
        self._calls.append(f"delete:{region_id}")
        self._rows.pop(region_id, None)

    def list_subscribed_region_ids(self) -> frozenset[int]:
        return frozenset(self._rows.keys())

    def seed(self, region_id: int, subscribed_at: datetime) -> None:
        self._rows[region_id] = subscribed_at


class FakeDndService:
    """Fake DndService that returns a fixed is_active result."""

    def __init__(self, *, active: bool = False) -> None:
        self._active = active

    def is_active(self, now: datetime) -> bool:
        return self._active

    def until(self, now: datetime) -> datetime | None:
        return None

    def set_dnd_until(self, now: datetime, minutes: int) -> None:
        pass


class FakeLotRepository:
    """In-memory lot repository for recovery tests."""

    def __init__(self) -> None:
        self._lots: dict[int, Any] = {}
        self._calls: list[str] = []

    def seed(self, lot: Any) -> None:
        self._lots[lot.id] = lot

    def get(self, lot_id: int) -> Any | None:
        self._calls.append(f"get:{lot_id}")
        return self._lots.get(lot_id)

    def upsert(self, lot: Any, *, tracked: Sequence[Any]) -> LotUpsertResult:
        self._calls.append("upsert")
        self._lots[lot.id] = lot
        return LotUpsertResult(was_new=True, changes=[])

    def list_active(self, *, limit: int, offset: int) -> list[Any]:
        self._calls.append("list_active")
        return list(self._lots.values())[offset : offset + limit]

    def get_last_known_id(self, region: int) -> int | None:
        self._calls.append(f"get_last_known_id:{region}")
        return None

    def set_last_known_id(self, region: int, value: int) -> None:
        self._calls.append(f"set_last_known_id:{region}:{value}")

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        self._calls.append("mark_seen")

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        self._calls.append("mark_inactive")

    def needing_enrichment(self, limit: int) -> list[int]:
        self._calls.append("needing_enrichment")
        return []


# ---------------------------------------------------------------------------
# FakeNotifier
# ---------------------------------------------------------------------------


class FakeNotifier:
    """Configurable fake Notifier that satisfies the Notifier Protocol.

    Pre-configured results are popped from a queue; falls back to ok=True
    when the queue is exhausted.
    """

    channel_id: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Fake Email"
    description: ClassVar[str] = "Fake notifier for tests"
    config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
    recipient_label: ClassVar[str] = "Email"
    recipient_placeholder: ClassVar[str] = "test@example.com"

    def __init__(self, channel_id: str = "email") -> None:
        # Override the ClassVar per-instance (needed to test multiple channels)
        type(self).channel_id = channel_id  # type: ignore[misc]
        self._results: list[NotifyResult] = []
        self.send_calls: list[tuple[Any, str]] = []  # (lot, recipient)
        self.test_calls: list[str] = []
        self._lock = threading.Lock()

    def queue_result(self, result: NotifyResult) -> None:
        """Pre-configure the next send() return value."""
        self._results.append(result)

    def send(self, lot: Any, recipient: str) -> NotifyResult:
        with self._lock:
            self.send_calls.append((lot, recipient))
        if self._results:
            return self._results.pop(0)
        return NotifyResult(ok=True, detail="sent", retryable=False)

    def test(self, recipient: str) -> NotifyResult:
        with self._lock:
            self.test_calls.append(recipient)
        return NotifyResult(ok=True, detail="test-ok", retryable=False)


# Needs separate class per distinct channel_id to avoid ClassVar collision
class FakeBrowserNotifier(FakeNotifier):
    channel_id: ClassVar[str] = "browser"
    display_name: ClassVar[str] = "Fake Browser"
    description: ClassVar[str] = "Fake browser notifier"
    config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
    recipient_label: ClassVar[str] = "Local"
    recipient_placeholder: ClassVar[str] = "local"

    def __init__(self) -> None:
        self._results: list[NotifyResult] = []
        self.send_calls: list[tuple[Any, str]] = []
        self.test_calls: list[str] = []
        self._lock = threading.Lock()


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _make_lot_public(lot_id: int = 12345, region_id: int | None = 1) -> LotPublicDTO:
    lot = make_lot(id=lot_id, region_id=region_id)
    return LotPublicDTO(
        **lot.model_dump(),
        age_seconds=3600,
        tier="match",
        freshness="warm",
    )


def _make_email_settings(recipients: list[str]) -> Settings:
    """Build Settings with a given email recipients list."""
    from fis_monitor.domain.models import (
        NotificationsConfig,
    )

    notif = NotificationsConfig(
        email=EmailConfig(enabled=True, recipients=recipients)  # type: ignore[arg-type]
    )
    return Settings(notifications=notif)


def _make_dispatcher(
    *,
    registry: ExplicitNotifierRegistry | None = None,
    notif_repo: FakeNotificationsRepository | None = None,
    lot_repo: FakeLotRepository | None = None,
    config_source: FakeConfigSource | None = None,
    clock: FakeClock | None = None,
    event_bus: FakeEventBus | None = None,
    stop_event: threading.Event | None = None,
    dnd_service: FakeDndService | None = None,
    retry_attempts: int = 3,
    retry_backoff: Sequence[float] = (0.01, 0.02, 0.04),  # tiny for fast tests
    max_queue_size: int = 100,
    recovery_age: timedelta = timedelta(minutes=1),
) -> tuple[
    NotifierDispatcher,
    ExplicitNotifierRegistry,
    FakeNotificationsRepository,
    FakeLotRepository,
    FakeConfigSource,
    FakeClock,
    FakeEventBus,
    threading.Event,
]:
    registry = registry or ExplicitNotifierRegistry()
    notif_repo = notif_repo or FakeNotificationsRepository()
    lot_repo = lot_repo or FakeLotRepository()
    config_source = config_source or FakeConfigSource()
    clock = clock or FakeClock()
    event_bus = event_bus or FakeEventBus()
    stop_event = stop_event or threading.Event()
    dnd_service = dnd_service or FakeDndService(active=False)
    dispatcher = NotifierDispatcher(
        registry=registry,
        notif_repo=notif_repo,
        lot_repo=lot_repo,
        config_source=config_source,
        clock=clock,
        event_bus=event_bus,
        stop_event=stop_event,
        dnd_service=dnd_service,
        retry_attempts=retry_attempts,
        retry_backoff=retry_backoff,
        max_queue_size=max_queue_size,
        recovery_age=recovery_age,
    )
    return dispatcher, registry, notif_repo, lot_repo, config_source, clock, event_bus, stop_event


# ===========================================================================
# Tests: dispatch()
# ===========================================================================


def test_dispatch_enqueues_lot():
    dispatcher, *_ = _make_dispatcher()
    lot = _make_lot_public()
    dispatcher.dispatch(lot)
    got = dispatcher._queue.get(timeout=0.1)
    assert got is lot


def test_dispatch_queue_full_drops_warns(caplog):
    dispatcher, *_ = _make_dispatcher(max_queue_size=1)
    lot1 = _make_lot_public(lot_id=1)
    lot2 = _make_lot_public(lot_id=2)
    dispatcher.dispatch(lot1)  # fills the single slot
    with caplog.at_level(logging.WARNING):
        dispatcher.dispatch(lot2)  # overflow — must not raise
    assert "dispatcher.queue_full" in caplog.text
    # Queue should still hold lot1, not lot2
    got = dispatcher._queue.get_nowait()
    assert got.id == 1


# ===========================================================================
# Tests: _send_one()
# ===========================================================================


def test_send_one_skip_if_already_sent():
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    # Pre-populate as sent
    notif_repo.seed_pending(lot.id, "email", "a@x.com", attempt_no=1)
    notif_repo._rows[(lot.id, "email", "a@x.com")]["status"] = "sent"

    dispatcher._send_one(lot, notifier, "a@x.com")
    assert len(notifier.send_calls) == 0


def test_send_one_skip_if_permanent_fail():
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notif_repo.seed_pending(lot.id, "email", "a@x.com")
    notif_repo._rows[(lot.id, "email", "a@x.com")]["status"] = "permanent_fail"

    dispatcher._send_one(lot, notifier, "a@x.com")
    assert len(notifier.send_calls) == 0


def test_send_one_reserves_then_attempts_then_marks_sent():
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    dispatcher._send_one(lot, notifier, "a@x.com")

    assert "reserve" in notif_repo._calls
    assert "mark_attempt" in notif_repo._calls
    assert "mark_sent" in notif_repo._calls
    assert len(notifier.send_calls) == 1
    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "sent"


def test_send_one_mark_attempt_returns_none_skips_send():
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notif_repo.seed_pending(lot.id, "email", "a@x.com")
    notif_repo._force_mark_attempt_none = True

    dispatcher._send_one(lot, notifier, "a@x.com")
    assert len(notifier.send_calls) == 0


def test_send_one_max_total_attempts_cap_marks_permanent_fail(caplog):
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    # Seed with attempt_no already at MAX so mark_attempt returns MAX+1
    notif_repo.seed_pending(lot.id, "email", "a@x.com", attempt_no=MAX_TOTAL_ATTEMPTS)

    with caplog.at_level(logging.WARNING):
        dispatcher._send_one(lot, notifier, "a@x.com")

    assert "notification.cap_reached" in caplog.text
    assert len(notifier.send_calls) == 0
    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "permanent_fail"


def test_send_one_retryable_failure_retries_with_backoff():
    """notifier returns retryable=True x2, then ok=True on 3rd attempt."""
    dispatcher, registry, notif_repo, *_ = _make_dispatcher(
        retry_attempts=3,
        retry_backoff=(0.001, 0.001, 0.001),
    )
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notifier.queue_result(NotifyResult(ok=False, detail="timeout", retryable=True))
    notifier.queue_result(NotifyResult(ok=False, detail="timeout", retryable=True))
    notifier.queue_result(NotifyResult(ok=True, detail="sent", retryable=False))

    dispatcher._send_one(lot, notifier, "a@x.com")

    assert len(notifier.send_calls) == 3
    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "sent"
    assert notif_repo.get_attempt_no(lot.id, "email", "a@x.com") == 3


def test_send_one_non_retryable_failure_marks_permanent_fail():
    dispatcher, registry, notif_repo, _, __, ___, event_bus, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notifier.queue_result(NotifyResult(ok=False, detail="auth_failed", retryable=False))

    dispatcher._send_one(lot, notifier, "a@x.com")

    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "permanent_fail"
    assert len(event_bus.smtp_failed_events()) == 1
    # Only 1 send attempt
    assert len(notifier.send_calls) == 1


def test_send_one_all_attempts_exhausted_does_not_permanent_fail():
    """All 3 attempts fail with retryable=True — status stays pending."""
    dispatcher, registry, notif_repo, _, __, ___, event_bus, *_ = _make_dispatcher(
        retry_attempts=3,
        retry_backoff=(0.001, 0.001, 0.001),
    )
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    for _ in range(3):
        notifier.queue_result(NotifyResult(ok=False, detail="timeout", retryable=True))

    dispatcher._send_one(lot, notifier, "a@x.com")

    # Status must remain pending (NOT permanent_fail) — recovery will retry
    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "pending"
    # smtp_failed event is published to alert operator
    assert len(event_bus.smtp_failed_events()) == 1


def test_send_one_stop_event_during_backoff_returns_immediately():
    """stop_event.set() during backoff sleep exits _send_one immediately."""
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, *_ = _make_dispatcher(
        stop_event=stop_event,
        retry_attempts=3,
        retry_backoff=(10.0, 10.0, 10.0),  # long — stop_event must cut short
    )
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    # Queue retryable failure so _send_one enters the backoff sleep
    notifier.queue_result(NotifyResult(ok=False, detail="timeout", retryable=True))

    def _set_stop_after_first_send() -> None:
        import time

        # Wait until at least one send call happened
        while len(notifier.send_calls) == 0:
            time.sleep(0.001)
        stop_event.set()

    t = threading.Thread(target=_set_stop_after_first_send, daemon=True)
    t.start()

    import time

    start = time.monotonic()
    dispatcher._send_one(lot, notifier, "a@x.com")
    elapsed = time.monotonic() - start

    t.join(timeout=2.0)
    # Should have returned well before the 10-second backoff expired
    assert elapsed < 5.0
    # Status still pending (recovery on next start)
    assert notif_repo.get_status(lot.id, "email", "a@x.com") == "pending"


# ===========================================================================
# Tests: consumer_loop()
# ===========================================================================


def _run_consumer(dispatcher: NotifierDispatcher, stop_event: threading.Event) -> threading.Thread:
    t = threading.Thread(target=dispatcher.consumer_loop, daemon=True)
    t.start()
    return t


def test_consumer_loop_drains_queue_until_stop():
    stop_event = threading.Event()
    dispatcher, registry, _notif_repo, *_ = _make_dispatcher(stop_event=stop_event)

    notifier = FakeNotifier("email")
    registry.register(notifier)

    settings = _make_email_settings(["a@x.com"])
    dispatcher._config_source = FakeConfigSource(settings)

    lots = [_make_lot_public(lot_id=i) for i in range(1, 6)]
    for lot in lots:
        dispatcher.dispatch(lot)

    t = _run_consumer(dispatcher, stop_event)

    import time

    deadline = time.monotonic() + 5.0
    while len(notifier.send_calls) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)

    stop_event.set()
    t.join(timeout=3.0)

    assert len(notifier.send_calls) == 5


def test_consumer_loop_recovery_processes_pending():
    """Pre-populated stale pending row is retried by consumer_loop recovery."""
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, lot_repo, *_ = _make_dispatcher(
        stop_event=stop_event,
        recovery_age=timedelta(minutes=1),
    )

    notifier = FakeNotifier("email")
    registry.register(notifier)

    lot_repo.seed(make_lot(id=999))

    # Stale pending: last_attempt_at older than recovery_age
    notif_repo.seed_pending(
        999,
        "email",
        "recover@x.com",
        attempt_no=1,
        last_attempt_at=_MINUTE_AGO - timedelta(seconds=30),
    )

    t = _run_consumer(dispatcher, stop_event)

    import time

    deadline = time.monotonic() + 5.0
    while len(notifier.send_calls) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    stop_event.set()
    t.join(timeout=3.0)

    assert len(notifier.send_calls) >= 1
    assert notif_repo.get_status(999, "email", "recover@x.com") == "sent"


def test_consumer_loop_recovery_zombie_null_last_attempt_at():
    """Zombie row (last_attempt_at=None, R4-C3) is also recovered."""
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, lot_repo, *_ = _make_dispatcher(stop_event=stop_event)

    notifier = FakeNotifier("email")
    registry.register(notifier)

    lot_repo.seed(make_lot(id=777))
    # Zombie: last_attempt_at=None
    notif_repo.seed_pending(777, "email", "zombie@x.com", last_attempt_at=None)

    t = _run_consumer(dispatcher, stop_event)

    import time

    deadline = time.monotonic() + 5.0
    while len(notifier.send_calls) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    stop_event.set()
    t.join(timeout=3.0)

    # Zombie was processed
    assert len(notifier.send_calls) >= 1


def test_consumer_loop_recovery_skip_unknown_channel(caplog):
    """Pending row for unregistered channel is skipped with a warning.

    Calls _retry_one directly to avoid queue.get(timeout=1.0) timing fragility.
    consumer_loop integration via queue draining is tested separately.
    """
    stop_event = threading.Event()
    dispatcher, _registry, notif_repo, lot_repo, *_ = _make_dispatcher(stop_event=stop_event)
    # Empty registry — no notifiers registered

    lot_repo.seed(make_lot(id=888))
    notif_repo.seed_pending(888, "email", "x@x.com")

    pending = notif_repo.list_pending_older_than(timedelta(minutes=0))
    assert len(pending) == 1

    with caplog.at_level(logging.WARNING, logger="fis_monitor.services.notifier_dispatcher"):
        dispatcher._retry_one(pending[0])

    assert "dispatcher.retry_unknown_channel" in caplog.text
    # Status stays pending (no permanent_fail for unknown channel)
    assert notif_repo.get_status(888, "email", "x@x.com") == "pending"


def test_retry_one_passes_lot_public_dto_not_lot(caplog):
    """_retry_one must convert Lot → LotPublicDTO before calling _send_one (P0-3).

    A bare Lot passed to BrowserSseNotifier.send() would trigger a Pydantic
    ValidationError inside the notifier, which would be swallowed as a
    non-retryable failure resulting in a false mark_sent.  This test verifies
    that the input to the notifier's send() is a LotPublicDTO, not a Lot.
    """
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, lot_repo, *_ = _make_dispatcher(stop_event=stop_event)

    notifier = FakeNotifier("email")
    registry.register(notifier)

    lot_repo.seed(make_lot(id=555))
    notif_repo.seed_pending(555, "email", "dto@x.com")

    pending = notif_repo.list_pending_older_than(timedelta(minutes=0))
    assert len(pending) == 1

    dispatcher._retry_one(pending[0])

    # Exactly one send call was made
    assert len(notifier.send_calls) == 1
    sent_lot, _recipient = notifier.send_calls[0]

    # The lot passed to send() must be a LotPublicDTO, not a bare Lot
    from fis_monitor.domain.models import Lot, LotPublicDTO

    assert isinstance(sent_lot, LotPublicDTO), (
        f"Expected LotPublicDTO but got {type(sent_lot).__name__} — "
        "bare Lot would break BrowserSseNotifier (P0-3 regression)"
    )
    assert type(sent_lot) is not Lot, "Must not be a plain Lot (only LotPublicDTO)"


def test_consumer_loop_recovery_lot_missing(caplog):
    """Pending row whose lot_id is not in lot_repo → permanent_fail + warning.

    Calls _retry_one directly to avoid queue.get(timeout=1.0) timing fragility.
    """
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, _lot_repo, *_ = _make_dispatcher(stop_event=stop_event)

    notifier = FakeNotifier("email")
    registry.register(notifier)
    # lot_repo is empty — lot does not exist

    notif_repo.seed_pending(666, "email", "ghost@x.com")

    pending = notif_repo.list_pending_older_than(timedelta(minutes=0))
    assert len(pending) == 1

    with caplog.at_level(logging.WARNING, logger="fis_monitor.services.notifier_dispatcher"):
        dispatcher._retry_one(pending[0])

    assert "dispatcher.retry_lot_missing" in caplog.text
    assert notif_repo.get_status(666, "email", "ghost@x.com") == "permanent_fail"
    assert len(notifier.send_calls) == 0


# ===========================================================================
# Tests: _dispatch_all_channels() and _recipients_of()
# ===========================================================================


def test_dispatch_all_channels_iterates_all_notifiers():
    """2 notifiers x 2 recipients = 4 send calls for 1 lot."""
    stop_event = threading.Event()
    dispatcher, registry, _notif_repo, *_ = _make_dispatcher(stop_event=stop_event)

    # Need separate classes to avoid ClassVar collision
    class NotifierA(FakeNotifier):
        channel_id: ClassVar[str] = "email"

    class NotifierB(FakeBrowserNotifier):
        channel_id: ClassVar[str] = "browser"

    na = NotifierA("email")
    nb = NotifierB()
    registry.register(na)
    registry.register(nb)

    # Email → 2 recipients
    settings = _make_email_settings(["a@x.com", "b@x.com"])
    dispatcher._config_source = FakeConfigSource(settings)

    lot = _make_lot_public()
    dispatcher._dispatch_all_channels(lot)

    # email: 2 recipients; browser: 1 recipient ("local") → total 3
    total = len(na.send_calls) + len(nb.send_calls)
    assert total == 3  # 2 email + 1 browser


def test_recipients_of_email_from_config_source():
    dispatcher, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")

    settings = _make_email_settings(["x@y.com", "z@y.com"])
    dispatcher._config_source = FakeConfigSource(settings)

    recipients = dispatcher._recipients_of(notifier)
    assert recipients == ["x@y.com", "z@y.com"]


def test_recipients_of_browser_returns_local():
    dispatcher, *_ = _make_dispatcher()
    notifier = FakeBrowserNotifier()
    recipients = dispatcher._recipients_of(notifier)
    assert recipients == ["local"]


def test_recipients_of_unknown_channel_empty():
    dispatcher, *_ = _make_dispatcher()

    class UnknownNotifier(FakeNotifier):
        channel_id: ClassVar[str] = "telegram"

    notifier = UnknownNotifier("telegram")
    recipients = dispatcher._recipients_of(notifier)
    assert recipients == []  # no raise — extensibility


def test_dispatch_suppressed_during_dnd_active():
    """When DnD is active, _dispatch_all_channels must not call any notifier.

    Acceptance criteria (P0-2):
    - FakeDndService.is_active() returns True.
    - Email notifier: 0 send calls.
    - Browser notifier: 0 send calls.
    - No notifications reserved in the repo.
    """
    dnd = FakeDndService(active=True)
    stop_event = threading.Event()
    dispatcher, registry, notif_repo, *_ = _make_dispatcher(
        stop_event=stop_event,
        dnd_service=dnd,
    )

    class _EmailNotifier(FakeNotifier):
        channel_id: ClassVar[str] = "email"

    class _BrowserNotifier(FakeBrowserNotifier):
        channel_id: ClassVar[str] = "browser"

    email_n = _EmailNotifier("email")
    browser_n = _BrowserNotifier()
    registry.register(email_n)
    registry.register(browser_n)

    settings = _make_email_settings(["a@x.com"])
    dispatcher._config_source = FakeConfigSource(settings)

    lot = _make_lot_public()
    dispatcher._dispatch_all_channels(lot)

    # No send calls on any notifier
    assert len(email_n.send_calls) == 0, "email notifier must not be called during DnD"
    assert len(browser_n.send_calls) == 0, "browser notifier must not be called during DnD"
    # No notifications reserved
    assert len(notif_repo._rows) == 0, "no rows must be reserved during DnD"


# ===========================================================================
# Tests: _publish_smtp_failed() PII safety
# ===========================================================================


def test_smtp_failed_event_published_with_pii_safe_detail(caplog):
    """SseSmtpFailed must not contain PII detail; audit log must contain it.

    Acceptance #2: detail (email-style PII) not in SSE payload.
    Acceptance #3: detail not in str(event).
    Acceptance #4: detail IS present in structured log record (audit trail).
    M3: error_category is a valid ErrorCategory member.
    """
    import typing

    from fis_monitor.domain.models import ErrorCategory

    dispatcher, registry, _nr, _, __, ___, event_bus, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    pii_detail = "secret@example.com"
    notifier.queue_result(NotifyResult(ok=False, detail=pii_detail, retryable=False))

    with caplog.at_level(logging.INFO, logger="fis_monitor.services.notifier_dispatcher"):
        dispatcher._send_one(lot, notifier, "recipient@example.com")

    events = event_bus.smtp_failed_events()
    assert len(events) == 1
    evt = events[0]

    # Acceptance #2-3: PII detail not in SSE event
    assert not hasattr(evt, "recipient")
    assert not hasattr(evt, "detail")
    assert pii_detail not in str(evt)

    # Acceptance #4: audit log record carries the detail (not stripped)
    log_records = [r for r in caplog.records if r.getMessage() == "dispatcher.smtp_failed"]
    assert log_records, "Expected at least one 'dispatcher.smtp_failed' log record"
    audit_record = log_records[0]
    assert audit_record.detail == pii_detail  # type: ignore[attr-defined]

    # channel_id and attempt_no are present in SSE
    assert evt.channel_id == "email"
    assert evt.attempt_no >= 1

    # M3: error_category is a valid closed-enum member
    valid_categories = typing.get_args(ErrorCategory)
    assert evt.error_category in valid_categories


def test_smtp_failed_detail_truncated_in_log(caplog):
    """Detail in structured log is truncated to 200 chars."""
    dispatcher, registry, _nr2, _, __, ___, _eb, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    long_detail = "A" * 300
    notifier.queue_result(NotifyResult(ok=False, detail=long_detail, retryable=False))

    with caplog.at_level(logging.WARNING, logger="fis_monitor.services.notifier_dispatcher"):
        dispatcher._send_one(lot, notifier, "target@example.com")

    # The full 300-char detail must not appear in logs
    assert "A" * 300 not in caplog.text
    # The truncated 200-char prefix IS present in the audit log record
    log_records = [r for r in caplog.records if r.getMessage() == "dispatcher.smtp_failed"]
    assert log_records, "Expected 'dispatcher.smtp_failed' log record"
    assert log_records[0].detail == "A" * 200  # type: ignore[attr-defined]
    assert "dispatcher.smtp_failed" in caplog.text


def test_pii_recipient_not_in_logs(caplog):
    """Recipient plaintext must never appear in any log output."""
    dispatcher, registry, _nr3, _, __, ___, _eb2, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notifier.queue_result(NotifyResult(ok=False, detail="err", retryable=False))

    with caplog.at_level(logging.DEBUG):
        dispatcher._send_one(lot, notifier, "private@example.com")

    assert "private@example.com" not in caplog.text


def test_cap_reached_log_does_not_contain_recipient(caplog):
    """notification.cap_reached log uses recipient_hash, not plaintext."""
    dispatcher, registry, notif_repo, *_ = _make_dispatcher()
    notifier = FakeNotifier("email")
    registry.register(notifier)
    lot = _make_lot_public()

    notif_repo.seed_pending(lot.id, "email", "vip@secret.com", attempt_no=MAX_TOTAL_ATTEMPTS)

    with caplog.at_level(logging.WARNING):
        dispatcher._send_one(lot, notifier, "vip@secret.com")

    assert "vip@secret.com" not in caplog.text
    assert "notification.cap_reached" in caplog.text


# ===========================================================================
# Anti-mock: all fake method surfaces exercised
# ===========================================================================


def test_all_fake_notifier_methods_invoked():
    """FakeNotifier: both send() and test() must be callable."""
    notifier = FakeNotifier("email")
    lot = _make_lot_public()

    result_send = notifier.send(lot, "a@x.com")
    assert result_send.ok is True

    result_test = notifier.test("a@x.com")
    assert result_test.ok is True

    assert len(notifier.send_calls) == 1
    assert len(notifier.test_calls) == 1


def test_all_fake_notifications_repo_methods_invoked():
    """Every method on FakeNotificationsRepository must be exercised."""
    repo = FakeNotificationsRepository()
    now = _NOW

    # reserve
    new = repo.reserve(1, "email", "a@x.com")
    assert new is True
    duplicate = repo.reserve(1, "email", "a@x.com")
    assert duplicate is False

    # status_of
    status = repo.status_of(1, "email", "a@x.com")
    assert status == "pending"

    # mark_attempt
    attempt_no = repo.mark_attempt(1, "email", "a@x.com", at=now)
    assert attempt_no == 1

    # mark_sent
    repo.mark_sent(1, "email", "a@x.com", at=now)
    assert repo.get_status(1, "email", "a@x.com") == "sent"

    # reserve again for permanent_fail test
    repo.reserve(2, "email", "b@x.com")
    # mark_permanent_fail
    repo.mark_permanent_fail(2, "email", "b@x.com")
    assert repo.get_status(2, "email", "b@x.com") == "permanent_fail"

    # list_pending_older_than (seeded stale row)
    repo.seed_pending(3, "email", "c@x.com", last_attempt_at=None)
    pending = repo.list_pending_older_than(timedelta(minutes=1))
    assert any(r.lot_id == 3 for r in pending)

    # list_recent
    recent = repo.list_recent(10)
    assert len(recent) >= 1

    # mark_attempt returns None for terminal status (R4-C4)
    result = repo.mark_attempt(1, "email", "a@x.com", at=now)  # already 'sent'
    assert result is None

    # All tracked method names exercised
    assert "reserve" in repo._calls
    assert "status_of" in repo._calls
    assert "mark_attempt" in repo._calls
    assert "mark_sent" in repo._calls
    assert "mark_permanent_fail" in repo._calls
    assert "list_pending_older_than" in repo._calls
    assert "list_recent" in repo._calls


def test_all_fake_lot_repo_methods_invoked():
    """Every method on FakeLotRepository must be exercised."""
    repo = FakeLotRepository()
    lot = make_lot(id=1)
    now = _NOW

    repo.seed(lot)
    assert repo.get(1) is lot
    assert repo.get(99) is None

    result = repo.upsert(lot, tracked=[])
    assert result.was_new is True

    active = repo.list_active(limit=10, offset=0)
    assert len(active) >= 1

    assert repo.get_last_known_id(1) is None
    repo.set_last_known_id(1, 100)

    repo.mark_seen([1], now)
    repo.mark_inactive(1, "hard_removed", now)

    enrichment = repo.needing_enrichment(10)
    assert isinstance(enrichment, list)


def test_all_fake_event_bus_methods_invoked():
    """FakeEventBus: publish() and subscribe() must be exercised."""
    bus = FakeEventBus()
    event = SseSmtpFailed(
        timestamp=_NOW,
        channel_id="email",
        attempt_no=1,
        error_category="network",
    )
    bus.publish(event)
    assert len(bus.published) == 1

    sub = bus.subscribe()
    assert sub is not None

    failed = bus.smtp_failed_events()
    assert len(failed) == 1


def test_all_fake_clock_methods_invoked():
    clock = FakeClock()
    now = clock.now()
    assert now == _NOW
    mono = clock.monotonic()
    assert isinstance(mono, float)


def test_all_fake_config_source_methods_invoked():
    source = FakeConfigSource()
    settings = source.current()
    assert isinstance(settings, Settings)

    sub = source.subscribe(lambda s: None)
    assert sub is not None
    sub.__enter__()
    sub.__exit__(None, None, None)
    sub.unsubscribe()


# ===========================================================================
# Tests: SubscribedAtFilteredNotifier (ADR-039) — per-channel email filter
# ===========================================================================

_MACRO_REGION_DFO = 1
# date_create == _NOW (2026-05-13 12:00 UTC) per factories._DEFAULT_NOW.
# ADR-039 fix (gn89): comparison is day-precision — `date()` of both sides.
_SUBSCRIBED_AT_PRIOR_DAY = _NOW - timedelta(days=1)  # day-12 → 13: pass (lot day > sub day)
_SUBSCRIBED_AT_SAME_DAY_EARLIER = _NOW - timedelta(hours=2)  # same day 10:00: pass (same day)
_SUBSCRIBED_AT_SAME_DAY_LATER = _NOW + timedelta(hours=1)  # same day 13:00: PASS (gn89 fix)
_SUBSCRIBED_AT_NEXT_DAY = _NOW + timedelta(days=1)  # day-14: SUPPRESS (lot day < sub day)


def test_filtered_notifier_suppresses_prior_day_lot(caplog):
    """Invariant 631y-1a (ADR-039 day-precision): lot.date_create.date() <
    subscribed_at.date() → send() suppressed, inner NOT called."""
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_NEXT_DAY)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)  # day-13 < day-14 → suppress

    with caplog.at_level(logging.DEBUG, logger="fis_monitor.services.notifier_dispatcher"):
        result = wrapper.send(lot, "test@example.com")

    assert result.ok is True
    assert "suppressed" in (result.detail or "")
    assert len(inner.send_calls) == 0
    assert "notification.subscribed_at_dropped" in caplog.text
    drop_records = [
        r for r in caplog.records if r.getMessage() == "notification.subscribed_at_dropped"
    ]
    assert drop_records
    assert drop_records[0].decision == "dropped_subscribed_at"  # type: ignore[attr-defined]


def test_filtered_notifier_passes_same_day_lot_subscribed_earlier_in_day():
    """ADR-039 gn89 fix: subscribed_at earlier the same day → same-day lot PASSES.

    This is the regression direction of the fix: previous timestamp-precision
    compare would have suppressed (lot=midnight < sub=10:00). Day-precision
    compare treats them as the same calendar day.
    """
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_SAME_DAY_EARLIER)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)

    result = wrapper.send(lot, "test@example.com")

    assert result.ok is True
    assert "suppressed" not in (result.detail or "")
    assert len(inner.send_calls) == 1


def test_filtered_notifier_passes_same_day_lot_subscribed_later_in_day():
    """ADR-039 gn89 fix (primary regression): user onboards mid-day, lot
    arrives later the same day — must be delivered.

    With the OLD timestamp-precision compare this scenario was the actual
    production bug: ``lot.date_create=00:00 < subscribed_at=13:00`` → all
    same-day lots silently suppressed. Day-precision compare delivers.
    """
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_SAME_DAY_LATER)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)

    result = wrapper.send(lot, "test@example.com")

    assert result.ok is True
    assert "suppressed" not in (result.detail or "")
    assert len(inner.send_calls) == 1


def test_filtered_notifier_passes_newer_lot():
    """date_create.date() > subscribed_at.date() → send() passes through."""
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_PRIOR_DAY)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)

    wrapper.send(lot, "test@example.com")

    assert len(inner.send_calls) == 1


def test_filtered_notifier_handles_midnight_lot_same_day_onboarding():
    """Production-fidelity edge case: ``date_create`` is midnight UTC (real
    parser output for "DD.MM.YYYY"). Onboarding the same day → must pass.

    This is the exact scenario from bd-task gn89 reproduction notes (lot
    666 was added the same day after onboarding and got silently dropped).
    """
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    onboarding = datetime(2026, 5, 17, 22, 31, 19, tzinfo=UTC)
    repo.seed(_MACRO_REGION_DFO, onboarding)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    # date_create as the parser produces it: midnight UTC of the same day.
    lot_data = make_lot(
        id=666,
        region_id=_MACRO_REGION_DFO,
        date_create=datetime(2026, 5, 17, tzinfo=UTC),
    )
    lot = LotPublicDTO(
        **lot_data.model_dump(),
        age_seconds=3600,
        tier="match",
        freshness="warm",
    )

    wrapper.send(lot, "test@example.com")

    assert len(inner.send_calls) == 1, "same-day lot must not be suppressed (gn89)"


def test_filtered_notifier_suppresses_no_subscribed_at(caplog):
    """get_subscribed_at returns None (known region, no record) → suppressed.

    Regression for prod-incident: lots from regions outside the subscription
    (subscribed_at=None for a known region_id) must NOT be delivered.
    Only lots with region_id=None (unrecognised subject) remain fail-open.
    """
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)

    with caplog.at_level(logging.DEBUG, logger="fis_monitor.services.notifier_dispatcher"):
        result = wrapper.send(lot, "test@example.com")

    assert result.ok is True
    assert "suppressed" in (result.detail or "")
    assert len(inner.send_calls) == 0
    assert "notification.subscribed_at_dropped" in caplog.text


def test_filtered_notifier_passes_region_id_none():
    """lot.region_id=None (legacy lot) → filter skipped, send() passes through (graceful)."""
    inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_NEXT_DAY)
    wrapper = SubscribedAtFilteredNotifier(inner=inner, region_sub_repo=repo)

    lot = _make_lot_public(region_id=None)

    wrapper.send(lot, "test@example.com")

    assert len(inner.send_calls) == 1


def test_dispatcher_old_lot_email_suppressed_browser_called():
    """Invariant 631y-1: old lot → email wrapper suppresses send; browser channel IS called.

    Additionally (gn89 fix): suppressed lots must NOT create a row in the
    notifications repository — pre-reserve ``should_suppress`` hook fires
    before ``reserve()`` so the audit log stays clean and recovery never
    sees a misleading ``status='sent'`` entry for an email that never went out.
    """
    email_inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_NEXT_DAY)
    filtered_email = SubscribedAtFilteredNotifier(inner=email_inner, region_sub_repo=repo)

    browser = FakeBrowserNotifier()

    registry = ExplicitNotifierRegistry()
    registry.register(filtered_email)
    registry.register(browser)

    config_source = FakeConfigSource(_make_email_settings(["user@example.com"]))
    dispatcher, _registry, notif_repo, *_ = _make_dispatcher(
        registry=registry, config_source=config_source
    )
    old_lot = _make_lot_public(region_id=_MACRO_REGION_DFO)  # day-13 < day-14 → suppress

    dispatcher.dispatch(old_lot)
    lot_from_q = dispatcher._queue.get_nowait()
    dispatcher._dispatch_all_channels(lot_from_q)

    assert len(email_inner.send_calls) == 0, "email must be suppressed for old lot"
    assert len(browser.send_calls) == 1, "browser must receive old lot"
    # gn89 secondary fix: no notifications row for suppressed email channel.
    email_status = notif_repo.status_of(old_lot.id, "email", "user@example.com")
    assert email_status is None, (
        "suppressed lots MUST NOT create a notifications row "
        "(no misleading status='sent', recovery-loop safe)"
    )


def test_dispatcher_new_lot_both_channels_called():
    """Invariant 631y-2: new lot → both email and browser channels called."""
    email_inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_PRIOR_DAY)
    filtered_email = SubscribedAtFilteredNotifier(inner=email_inner, region_sub_repo=repo)

    browser = FakeBrowserNotifier()

    registry = ExplicitNotifierRegistry()
    registry.register(filtered_email)
    registry.register(browser)

    config_source = FakeConfigSource(_make_email_settings(["user@example.com"]))
    dispatcher, *_ = _make_dispatcher(registry=registry, config_source=config_source)

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)  # day-13 > day-12 → pass

    dispatcher.dispatch(lot)
    lot_from_q = dispatcher._queue.get_nowait()
    dispatcher._dispatch_all_channels(lot_from_q)

    assert len(email_inner.send_calls) == 1, "email must be called for new lot"
    assert len(browser.send_calls) == 1, "browser must be called for new lot"


def test_dispatcher_same_day_lot_passes_both_channels():
    """gn89 regression test (dispatcher level): same-day lot must reach BOTH
    email and browser channels, and the email row in notifications must be
    marked sent (not suppressed).
    """
    email_inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_SAME_DAY_EARLIER)
    filtered_email = SubscribedAtFilteredNotifier(inner=email_inner, region_sub_repo=repo)

    browser = FakeBrowserNotifier()

    registry = ExplicitNotifierRegistry()
    registry.register(filtered_email)
    registry.register(browser)

    config_source = FakeConfigSource(_make_email_settings(["user@example.com"]))
    dispatcher, _registry, notif_repo, *_ = _make_dispatcher(
        registry=registry, config_source=config_source
    )
    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)

    dispatcher.dispatch(lot)
    lot_from_q = dispatcher._queue.get_nowait()
    dispatcher._dispatch_all_channels(lot_from_q)

    assert len(email_inner.send_calls) == 1, "same-day lot must reach email"
    assert len(browser.send_calls) == 1, "same-day lot must reach browser"
    assert notif_repo.status_of(lot.id, "email", "user@example.com") == "sent"


def test_dispatcher_recovery_path_suppressed_pending_row_promoted_to_permanent_fail():
    """gn89 recovery-zombie regression: if ``_retry_one`` invokes ``_send_one``
    on an already-reserved (``pending``) row and ``should_suppress`` fires
    (e.g. ``subscribed_at`` was reset to a future date after the row was
    reserved), the row MUST be promoted to ``permanent_fail`` so the
    recovery sweep stops re-picking it up indefinitely.
    """
    email_inner = FakeNotifier(channel_id="email")
    repo = FakeRegionSubscriptionRepository()
    repo.seed(_MACRO_REGION_DFO, _SUBSCRIBED_AT_NEXT_DAY)
    filtered_email = SubscribedAtFilteredNotifier(inner=email_inner, region_sub_repo=repo)

    registry = ExplicitNotifierRegistry()
    registry.register(filtered_email)

    config_source = FakeConfigSource(_make_email_settings(["user@example.com"]))
    dispatcher, _registry, notif_repo, *_ = _make_dispatcher(
        registry=registry, config_source=config_source
    )

    lot = _make_lot_public(region_id=_MACRO_REGION_DFO)
    # Pre-seed the notifications row as if a prior delivery attempt reserved
    # it BEFORE suppression criteria changed (the zombie scenario).
    notif_repo.reserve(lot.id, "email", "user@example.com")
    assert notif_repo.status_of(lot.id, "email", "user@example.com") == "pending"

    # Now suppression fires (subscribed_at moved to future). Recovery-path
    # _send_one must terminate the row, not leave it as a sweep-magnet.
    dispatcher._send_one(lot, filtered_email, "user@example.com")

    assert len(email_inner.send_calls) == 0, "suppressed lot must not be sent"
    assert notif_repo.status_of(lot.id, "email", "user@example.com") == "permanent_fail", (
        "stuck pending row must be promoted to permanent_fail to exit recovery"
    )


# ===========================================================================
# Tests: RfSubjectFilteredEmailNotifier (ADR-035 + scrd) — email notify-scope
# ===========================================================================

_REGION_KHABAROVSK = 89  # default region in tests.factories.make_lot


def _make_filters_settings(rf_subjects: list[int]) -> Settings:
    return Settings(
        notifications=NotificationsConfig(
            email=EmailConfig(enabled=True, recipients=["user@example.com"])  # type: ignore[arg-type]
        ),
        filters=FiltersConfig(rf_subjects=rf_subjects),
    )


def test_rf_filter_should_suppress_when_region_outside_rf_subjects(caplog):
    """scrd primary invariant: lot from a region NOT in filters.rf_subjects
    is suppressed for the email channel."""
    inner = FakeNotifier(channel_id="email")
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[1]))  # not 89
    wrapper = RfSubjectFilteredEmailNotifier(
        inner=inner, config_source=config, matcher=RfSubjectFilterMatcher()
    )
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    with caplog.at_level(logging.DEBUG, logger="fis_monitor.services.notifier_dispatcher"):
        result = wrapper.send(lot, "user@example.com")

    assert result.ok is True
    assert "suppressed" in (result.detail or "")
    assert len(inner.send_calls) == 0
    assert "notification.rf_subjects_dropped" in caplog.text


def test_rf_filter_passes_when_region_inside_rf_subjects():
    """Region IS in rf_subjects → email passes through to inner notifier."""
    inner = FakeNotifier(channel_id="email")
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[_REGION_KHABAROVSK]))
    wrapper = RfSubjectFilteredEmailNotifier(
        inner=inner, config_source=config, matcher=RfSubjectFilterMatcher()
    )
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    wrapper.send(lot, "user@example.com")

    assert len(inner.send_calls) == 1


def test_rf_filter_empty_rf_subjects_passes_all_lots():
    """Empty rf_subjects list = no notify-scope filter → pass-through."""
    inner = FakeNotifier(channel_id="email")
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[]))
    wrapper = RfSubjectFilteredEmailNotifier(
        inner=inner, config_source=config, matcher=RfSubjectFilterMatcher()
    )
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    wrapper.send(lot, "user@example.com")

    assert len(inner.send_calls) == 1


def test_rf_empty_with_subscribed_at_passes_new_lots():
    """ADR-035 I4 + ADR-039 composite invariant (variant b confirmed by user).

    Empty rf_subjects = notify-all (no region restriction), but
    SubscribedAtFilteredNotifier still applies the subscribed_at cutoff.
    A lot whose date_create >= subscribed_at must reach the inner notifier.
    """
    # Given
    smtp = FakeNotifier(channel_id="email")
    region_repo = FakeRegionSubscriptionRepository()
    subscribed_at = _NOW - timedelta(days=1)  # subscribed yesterday
    region_repo.seed(_REGION_KHABAROVSK, subscribed_at)

    subscribed_wrapper = SubscribedAtFilteredNotifier(inner=smtp, region_sub_repo=region_repo)
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[]))  # notify-all
    outer = RfSubjectFilteredEmailNotifier(
        inner=subscribed_wrapper, config_source=config, matcher=RfSubjectFilterMatcher()
    )

    # lot created today — AFTER subscribed_at
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)
    # _make_lot_public uses make_lot which defaults date_create to _DEFAULT_NOW (_NOW)

    # When
    result = outer.send(lot, "user@example.com")

    # Then
    assert result.ok is True
    assert (result.detail or "") != "suppressed (subscribed_at)"
    assert len(smtp.send_calls) == 1, "new lot must pass through to smtp"


def test_rf_empty_with_subscribed_at_drops_old_lots():
    """ADR-035 I4 + ADR-039 composite invariant (variant b confirmed by user).

    Empty rf_subjects lets all regions through the RF decorator, but
    SubscribedAtFilteredNotifier must still suppress lots created BEFORE
    subscribed_at (onboarding anti-spam cutoff by design).
    """
    # Given
    smtp = FakeNotifier(channel_id="email")
    region_repo = FakeRegionSubscriptionRepository()
    subscribed_at = _NOW + timedelta(days=1)  # subscribed tomorrow → today's lot is old
    region_repo.seed(_REGION_KHABAROVSK, subscribed_at)

    subscribed_wrapper = SubscribedAtFilteredNotifier(inner=smtp, region_sub_repo=region_repo)
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[]))  # notify-all
    outer = RfSubjectFilteredEmailNotifier(
        inner=subscribed_wrapper, config_source=config, matcher=RfSubjectFilterMatcher()
    )

    # lot created today — strictly BEFORE subscribed_at (tomorrow)
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    # When
    result = outer.send(lot, "user@example.com")

    # Then: suppressed by subscribed_at filter (not by rf_subjects)
    assert result.ok is True
    assert "suppressed" in (result.detail or ""), "lot must be suppressed"
    assert len(smtp.send_calls) == 0, "smtp must not be called for old lot"


def test_rf_non_empty_with_unselected_region_drops_before_subscribed_at():
    """ADR-035 I5: when rf_subjects is set and lot region is NOT in it,
    suppression fires at the RF layer — SubscribedAtFilteredNotifier inner
    should_suppress is NOT invoked (short-circuit at the outer decorator).

    Concretely: region_Y lot with rf_subjects=[region_X] is suppressed on
    the rf_subjects check; the subscribed_at layer is never reached.
    """
    # Given — two distinct regions
    region_x = 77  # Moscow — in rf_subjects
    region_y = _REGION_KHABAROVSK  # 89 — NOT in rf_subjects

    smtp = FakeNotifier(channel_id="email")
    region_repo = FakeRegionSubscriptionRepository()
    # seed subscribed_at for region_y (so if it WERE checked, it would suppress)
    region_repo.seed(region_y, _NOW + timedelta(days=1))

    subscribed_wrapper = SubscribedAtFilteredNotifier(inner=smtp, region_sub_repo=region_repo)
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[region_x]))
    outer = RfSubjectFilteredEmailNotifier(
        inner=subscribed_wrapper, config_source=config, matcher=RfSubjectFilterMatcher()
    )

    lot = _make_lot_public(region_id=region_y)  # region NOT in rf_subjects

    # When
    result = outer.send(lot, "user@example.com")

    # Then: suppressed by rf_subjects (not subscribed_at)
    assert result.ok is True
    assert "suppressed" in (result.detail or ""), "lot must be suppressed at RF layer"
    assert len(smtp.send_calls) == 0, "smtp must not be called"
    # subscribed_at layer was not reached — region_repo has no get_subscribed_at call
    # for region_y via the subscribed_wrapper path (it was short-circuited by rf filter)
    subscribed_at_calls = [c for c in region_repo._calls if f"get_subscribed_at:{region_y}" in c]
    assert subscribed_at_calls == [], (
        "get_subscribed_at must NOT be called when rf_subjects suppresses first"
    )


def test_rf_filter_chains_inner_should_suppress():
    """Composite chain (Rf → SubscribedAt → smtp): if rf passes but inner
    subscribed_at suppresses, the OUTER ``should_suppress`` must reflect
    the inner verdict so the dispatcher's pre-reserve hook fires.
    """
    smtp = FakeNotifier(channel_id="email")
    region_repo = FakeRegionSubscriptionRepository()
    region_repo.seed(_REGION_KHABAROVSK, _NOW + timedelta(days=1))  # next-day → suppress
    subscribed_wrapper = SubscribedAtFilteredNotifier(inner=smtp, region_sub_repo=region_repo)
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[_REGION_KHABAROVSK]))
    outer = RfSubjectFilteredEmailNotifier(
        inner=subscribed_wrapper, config_source=config, matcher=RfSubjectFilterMatcher()
    )
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    assert outer.should_suppress(lot) is True
    assert len(smtp.send_calls) == 0


def test_dispatcher_rf_filtered_email_no_db_row_browser_called():
    """scrd regression at the dispatcher level: a lot from a non-subscribed
    region must (a) NOT create a notifications row for email, (b) STILL
    reach the browser channel for live UI.
    """
    smtp = FakeNotifier(channel_id="email")
    region_repo = FakeRegionSubscriptionRepository()
    config = FakeConfigSource(_make_filters_settings(rf_subjects=[1]))  # not 89
    email_wrapper = RfSubjectFilteredEmailNotifier(
        inner=SubscribedAtFilteredNotifier(inner=smtp, region_sub_repo=region_repo),
        config_source=config,
        matcher=RfSubjectFilterMatcher(),
    )
    browser = FakeBrowserNotifier()

    registry = ExplicitNotifierRegistry()
    registry.register(email_wrapper)
    registry.register(browser)

    dispatcher, _registry, notif_repo, *_ = _make_dispatcher(
        registry=registry, config_source=config
    )
    lot = _make_lot_public(region_id=_REGION_KHABAROVSK)

    dispatcher.dispatch(lot)
    lot_from_q = dispatcher._queue.get_nowait()
    dispatcher._dispatch_all_channels(lot_from_q)

    assert len(smtp.send_calls) == 0, "email must be suppressed for non-subscribed region"
    assert len(browser.send_calls) == 1, "browser must receive the lot regardless of rf_subjects"
    assert notif_repo.status_of(lot.id, "email", "user@example.com") is None, (
        "no notifications row for rf_subjects-suppressed lot"
    )


# ===========================================================================
# Anti-mock: FakeRegionSubscriptionRepository all methods exercised
# ===========================================================================


def test_all_fake_region_sub_repo_methods_invoked():
    """Every method on FakeRegionSubscriptionRepository must be callable."""
    repo = FakeRegionSubscriptionRepository()

    inserted = repo.set_if_absent(89, _NOW)
    assert inserted is True

    inserted2 = repo.set_if_absent(89, _NOW)
    assert inserted2 is False

    result = repo.get_subscribed_at(89)
    assert result == _NOW

    result_none = repo.get_subscribed_at(999)
    assert result_none is None

    repo.delete(89)
    assert repo.get_subscribed_at(89) is None

    repo.delete(89)  # idempotent

    ids = repo.list_subscribed_region_ids()
    assert isinstance(ids, frozenset)

    assert "get_subscribed_at:89" in repo._calls
    assert "set_if_absent:89" in repo._calls
    assert "delete:89" in repo._calls
