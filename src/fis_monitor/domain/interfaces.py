"""Domain Protocol interfaces — all seams of the fis-monitor architecture.

This module is the SINGLE source of truth for every structural Protocol
(typing.Protocol) that separates domain/services from infrastructure.

Layer topology (docs/architecture/03-protocols.md, docs/architecture/06-notifier-registry.md):
  Layer 0 — core system utilities:  Clock, ConnectionProvider, Locker,
                                     ConfigSource, EventBus
  Layer 1 — persistence repositories: LotRepository, UserStateRepository,
                                        NotificationsRepository,
                                        SettingsRepository,
                                        SmtpCredentialsRepository,
                                        CyclesRepository
  Layer 2 — external-system adapters: HttpClient, ListParser, DetailParser,
                                       LoginSession, SmtpHostPolicy,
                                       AutostartManager, MigrationRunner
  Layer 3 — notification plugin:       Notifier

Design rules (enforced by import-linter, docs/decisions/ADR-006-import-linter-ci.md):
  - This module MUST NOT import from infra / services / web / composition.
  - Only stdlib + pydantic + domain siblings (models, errors) are allowed.

@runtime_checkable:
  Applied ONLY on Notifier (registry isinstance-check) and Clock (test
  duck-typing probe). All other Protocols are structural-only — no
  runtime isinstance needed, and the decorator has non-trivial overhead.

Subscription handles (follow-up z9d):
  EventSubscription[T] and ConfigSubscription moved here from models.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from typing import (
    Any,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)

from fis_monitor.domain.models import (
    CycleResult,
    LotPublicDTO,
    LotUpsertResult,
    LotUserState,
    NotificationRecord,
    NotifierConfig,
    NotifyResult,
    OnboardingState,
    ParsedDetail,
    ParsedListRow,
    ResolvedSmtpEndpoint,
    Settings,
    SmtpCredentials,
    TrackedField,
)
from fis_monitor.domain.models import (
    HttpResponse as HttpResponse,
)
from fis_monitor.domain.models import (
    LockHandle as LockHandle,
)
from fis_monitor.domain.models import (
    Lot as Lot,
)
from fis_monitor.domain.models import (
    SseEvent as SseEvent,
)

__all__ = [
    "AutostartManager",
    "Clock",
    "ConfigSource",
    "ConfigSubscription",
    "ConnectionProvider",
    "CyclesRepository",
    "DetailParser",
    "EventBus",
    "EventSubscription",
    "HttpClient",
    "ListParser",
    "Locker",
    "LoginSession",
    "LotRepository",
    "MigrationRunner",
    "NotificationsRepository",
    "Notifier",
    "SettingsRepository",
    "SmtpCredentialsRepository",
    "SmtpHostPolicy",
    "UserStateRepository",
]

# ---------------------------------------------------------------------------
# Subscription handles — moved from models.py (follow-up z9d)
# ---------------------------------------------------------------------------
class EventSubscription[T](Protocol):
    """Handle returned by `EventBus.subscribe()`. Context-manager + lazy
    iterator over received events.

    Generic over the event type ``T`` so call-sites can write
    ``EventSubscription[SseEvent]`` and rely on static type-checking of
    the iterator yield-type.

    Invariant: ``iter()`` is a non-blocking generator yielding events as
    they arrive. Concrete bus implementations decide the back-pressure
    policy (drop-from-tail for ``normal``, force-unsubscribe for slow
    consumers of ``critical`` — see docs/architecture/03-protocols.md §3.5 / R3-C5).

    ``unsubscribe()`` MUST be idempotent: calling it after ``__exit__``
    (or twice in a row) is a no-op.
    """

    def __enter__(self) -> EventSubscription[T]: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...
    def unsubscribe(self) -> None: ...
    def iter(self) -> Iterator[T]: ...


class ConfigSubscription(Protocol):
    """Context-manager handle returned by ``ConfigSource.subscribe(cb)``.

    Separate type from ``EventSubscription`` to keep call-sites
    unambiguous — config reload and SSE bus events are different
    lifecycles. Reload delivery is push-based (callback), so this handle
    has no ``iter()``.
    """

    def __enter__(self) -> ConfigSubscription: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...
    def unsubscribe(self) -> None: ...


# ===========================================================================
# Layer 0 — core system utilities
# ===========================================================================


@runtime_checkable
class Clock(Protocol):
    """Wall-clock and monotonic time source.

    Injected into every service that needs deterministic time in tests.
    Implementation: ``infra/clock.py::SystemClock``.
    """

    def now(self) -> datetime:
        """Return current aware datetime in UTC."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically-increasing float (seconds), like ``time.monotonic``."""
        ...


class ConnectionProvider(Protocol):
    """Per-thread SQLite connection factory.

    NOTE (docs/architecture/03-protocols.md §3 note): ``ConnectionProvider`` is an
    infra-internal seam, not a domain concept. It is defined here so that
    repository Protocols can be tested with fake providers, but callers
    outside ``infra/`` MUST NOT cast raw ``sqlite3.Connection`` objects.

    Implementation: ``infra/sqlite/connection.py::ThreadLocalConnectionProvider``.
    """

    def get(self) -> Any:  # returns sqlite3.Connection; typed as Any to avoid sqlite3 import
        """Return the per-thread connection, creating one if necessary."""
        ...

    def close_all(self) -> None:
        """Close every open per-thread connection (used in shutdown)."""
        ...


class Locker(Protocol):
    """Single-instance OS-level lock.

    Invariant: implementation MUST use an OS-level lock
    (``fcntl.flock`` on Linux, ``msvcrt.locking`` on Windows) with
    ``O_NOFOLLOW|O_EXCL``. The PID stored in the lock-file is for human
    inspection only — it is NOT used for arbitration.

    Implementation: ``infra/lock.py::FileLocker``.
    """

    def acquire(self) -> LockHandle:
        """Acquire the lock. Raises ``AlreadyRunningError`` if another instance holds it."""
        ...

    def release(self, handle: LockHandle) -> None:
        """Release the lock and unlink the lock-file."""
        ...


class ConfigSource(Protocol):
    """Live configuration stream with hot-reload.

    Implementation: ``infra/config_source.py::WatchdogConfigSource``.
    """

    def current(self) -> Settings:
        """Return the most recently loaded ``Settings`` snapshot."""
        ...

    def subscribe(self, cb: Callable[[Settings], None]) -> ConfigSubscription:
        """Register a callback invoked on every config reload.

        Returns a ``ConfigSubscription`` context-manager handle.
        Callers SHOULD use it as a context manager to guarantee
        ``unsubscribe()`` is called even on exceptions.
        """
        ...


class EventBus(Protocol):
    """sync→async bridge for SSE fan-out.

    A single ``publish()`` method reads ``event.priority`` (ClassVar) to
    decide dispatch strategy (OCP — adding a new event type does NOT
    change ``EventBus``).

    normal priority:   ``put_nowait``; drop-from-tail when per-subscriber
                       queue reaches ``maxsize=100``.
    critical priority: blocking ``put(timeout=2.0)``; force-unsubscribe
                       slow consumers; persist per-type
                       ``last_critical_event:*`` rows in ``state`` table
                       (TTL 1 hour, per-type slots — R3-C5).

    Implementation: ``infra/sse/bus.py::ThreadEventBus``.
    """

    def publish(self, event: SseEvent) -> None:
        """Publish ``event`` to all subscribers."""
        ...

    def subscribe(self) -> EventSubscription[SseEvent]:
        """Return a per-subscriber context-manager handle."""
        ...


# ===========================================================================
# Layer 1 — persistence repositories
# ===========================================================================


class LotRepository(Protocol):
    """Primary lot persistence seam.

    All read-then-write operations use ``BEGIN IMMEDIATE`` to capture the
    writer-lock before the first ``SELECT`` (eliminates TOCTOU between
    ``SELECT old`` and ``UPDATE``).
    See docs/decisions/ADR-016-repository-invariants-begin-immediate.md,
    docs/architecture/03-protocols.md §3.1.
    """

    def upsert(self, lot: Lot, *, tracked: Sequence[TrackedField]) -> LotUpsertResult:
        """Atomically insert-or-update ``lot`` and record field-level history.

        The implementation calls ``compute_changes(old, lot, tracked)``
        inside the same ``BEGIN IMMEDIATE`` transaction — callers MUST NOT
        compute the diff themselves beforehand (R3-C2).
        """
        ...

    def get(self, lot_id: int) -> Lot | None: ...
    def list_active(self, *, limit: int, offset: int) -> list[Lot]: ...
    def get_last_known_id(self, region: int) -> int | None: ...
    def set_last_known_id(self, region: int, value: int) -> None: ...  # BEGIN IMMEDIATE

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None: ...
    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None: ...  # BEGIN IMMEDIATE

    def needing_enrichment(self, limit: int) -> list[int]:
        """Return lot-ids waiting for detail enrichment.

        The implementation MUST fetch into a list and close the cursor
        before returning (never exposes an open cursor to callers).
        """
        ...


class UserStateRepository(Protocol):
    """Per-lot user interaction state (starred, submitted, notes, visits)."""

    def get(self, lot_id: int) -> LotUserState | None: ...
    def set_starred(self, lot_id: int, value: bool) -> None: ...
    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None: ...
    def set_note(self, lot_id: int, note: str | None) -> None: ...

    def mark_visited(self, at: datetime) -> None:
        """Record the timestamp of the user's most recent dashboard visit.

        This is a **global** (single-valued) timestamp — it tracks the last
        time the user opened the dashboard, not a per-lot visit counter.
        For per-lot interaction state (starred, submitted, notes) use the
        other methods on this repository.
        """
        ...

    def last_visit(self) -> datetime | None:
        """Return the timestamp of the last ``mark_visited()`` call, or
        ``None`` if the dashboard has never been visited.

        Global, not per-lot — mirrors the semantics of ``mark_visited()``.
        """
        ...


class NotificationsRepository(Protocol):
    """Idempotency + state-machine for notification delivery.

    Primary key: ``(lot_id, channel, recipient)``.
    State machine: ``pending → sent | permanent_fail``.

    All methods execute inside their own short ``BEGIN IMMEDIATE``
    transaction — no two methods share a transaction.
    See ADR-019, notifications.md.
    """

    def reserve(self, lot_id: int, channel: str, recipient: str) -> bool:
        """Create slot if it does not exist (``INSERT OR IGNORE``).

        Returns ``True`` if a new row was created, ``False`` if the slot
        already existed (any status).
        """
        ...

    def status_of(
        self, lot_id: int, channel: str, recipient: str
    ) -> Literal["pending", "sent", "permanent_fail"] | None:
        """Return the current status, or ``None`` if no slot exists yet."""
        ...

    def mark_attempt(
        self, lot_id: int, channel: str, recipient: str, at: datetime
    ) -> int | None:
        """Increment ``attempt_no`` and set ``last_attempt_at = at``.

        Returns the new ``attempt_no`` on success.
        Returns ``None`` if the row is already in a terminal status
        (``sent`` or ``permanent_fail``) — a legitimate race with a
        concurrent consumer or recovery loop; callers MUST skip the send.
        (R4-C4, notifications.md)
        """
        ...

    def mark_sent(self, lot_id: int, channel: str, recipient: str, at: datetime) -> None: ...
    def mark_permanent_fail(self, lot_id: int, channel: str, recipient: str) -> None: ...

    def list_pending_older_than(self, age: timedelta) -> list[NotificationRecord]:
        """Recovery query: rows with ``status='pending'`` and
        ``last_attempt_at < now() - age`` (includes NULL ``last_attempt_at``
        zombie-reserves — R4-C3).
        """
        ...

    def list_recent(self, limit: int) -> list[NotificationRecord]: ...


class SettingsRepository(Protocol):
    """Key/value ``state`` table — onboarding progress, session flags, etc."""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def get_onboarding(self) -> OnboardingState: ...
    def set_onboarding(self, st: OnboardingState) -> None: ...


class SmtpCredentialsRepository(Protocol):
    """Singleton SMTP credentials stored in ``state.db`` (ADR-020).

    ``save()`` is an upsert against the singleton row (``id=1``).
    """

    def load(self) -> SmtpCredentials | None: ...

    def save(self, creds: SmtpCredentials) -> None:
        """Atomically replace the singleton row (``BEGIN IMMEDIATE; INSERT OR REPLACE``)."""
        ...


class CyclesRepository(Protocol):
    """Monitor-cycle lifecycle tracking (``cycles`` table)."""

    def open(self, region: int, at: datetime) -> int:
        """Insert an open cycle row and return the new ``cycle_id``."""
        ...

    def close(self, cycle_id: int, result: CycleResult) -> None: ...
    def list_recent(self, limit: int) -> list[CycleResult]: ...


# ===========================================================================
# Layer 2 — external-system adapters
# ===========================================================================


class HttpClient(Protocol):
    """Synchronous HTTP GET seam.

    Implementation: ``infra/http/client.py::RequestsHttpClient``
    (persistent ``requests.Session`` per thread, cookies from Playwright
    ``profile/``).
    """

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...


class ListParser(Protocol):
    """Parse the lot-list HTML page into structured rows."""

    def parse(self, html: str) -> list[ParsedListRow]: ...


class DetailParser(Protocol):
    """Parse a single lot detail-card HTML into a ``ParsedDetail``."""

    def parse(self, html: str) -> ParsedDetail: ...


class LoginSession(Protocol):
    """Headed-login via Playwright (no other responsibilities).

    Implementation: ``infra/playwright/login.py::PlaywrightLoginSession``.

    Invariant (zafixirovano in integration test): implementation MUST
    register ``context.route()`` with a host-whitelist and abort all
    other requests.
    """

    def open_headed_login(self, *, deadline: float) -> Any:
        """Blocking call: open a browser window, wait for the ФИС redirect.

        ``deadline`` — wall-clock seconds until a hard timeout.
        Returns ``LoginOutcome``.
        """
        ...

    def cancel(self) -> None:
        """Thread-safe external stop. Calls ``browser.close()`` from outside
        the worker thread. Safe to call when no job is active (no-op).
        """
        ...


class SmtpHostPolicy(Protocol):
    """DNS-resolve + security policy check for SMTP endpoints (ADR-015, ADR-021).

    Separates policy validation (what is a "safe host") from format
    validation (``SmtpCredentials`` Pydantic model). Domain never imports
    DNS or cloud-metadata block-lists.
    """

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        """Resolve ``host`` and validate all returned A/AAAA addresses.

        Raises ``SmtpHostPolicyError`` if any address fails the policy
        (private IP, loopback, cloud-metadata endpoint, blocked TLD, …).
        Returns a ``ResolvedSmtpEndpoint`` pinned to the first valid address —
        callers MUST use ``endpoint.ip`` for the actual ``socket.connect()``
        to eliminate TOCTOU between resolve and connect (R3-C4).
        """
        ...


class AutostartManager(Protocol):
    """OS-level autostart management (Windows Task Scheduler / XDG Autostart).

    Implementations: ``infra/autostart/windows.py::WindowsAutostart``,
    ``infra/autostart/linux.py::LinuxAutostart``.
    """

    def is_enabled(self) -> bool: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...


class MigrationRunner(Protocol):
    """Schema migration runner (state.db ``user_version`` lifecycle).

    Driven by ``init_db()`` at startup when DB ``user_version`` is older
    than the application's ``latest_version``. The concrete implementation
    (``infra/sqlite/migrations.SqliteMigrationRunner``) also exposes
    ``__call__(conn, from_version, to_version)`` so it satisfies the
    ``Callable[[Connection, int, int], None]`` signature accepted by
    ``init_db``.

    Contract for ``run_pending`` implementations:
      * First action MUST be ``BEGIN IMMEDIATE`` on the supplied connection.
      * After acquiring the writer lock, re-check ``PRAGMA user_version`` —
        on mismatch raise ``ConcurrentMigrationError`` (TOCTOU defence,
        bd issue ``1zk``).
      * Apply every ``Migration.apply(conn)`` step inside the same tx.
      * Update ``PRAGMA user_version`` to ``to_version`` in the same tx,
        then ``COMMIT``. On any exception ``ROLLBACK`` and re-raise.
    """

    def list_migrations(self) -> Sequence[Any]:
        """Return all registered migrations, ordered by ``from_version`` asc.

        Returns ``Sequence[Migration]`` — typed as ``Sequence[Any]`` here
        because ``Migration`` is an infra dataclass (``infra/sqlite/migrations``)
        that depends on ``sqlite3.Connection``; the domain layer must not
        import infra. Concrete implementations narrow the return type.
        """
        ...

    def run_pending(
        self, conn: Any, from_version: int, to_version: int
    ) -> None:
        """Apply chained migrations to take DB from ``from_version`` → ``to_version``.

        ``conn`` is typed ``Any`` here to avoid importing ``sqlite3`` into
        the domain layer; concrete implementations type it as
        ``sqlite3.Connection``.

        Raises:
            ConcurrentMigrationError: user_version moved between init_db
                read and BEGIN IMMEDIATE.
            MigrationChainBroken: no continuous chain registered.
        """
        ...

    def __call__(
        self, conn: Any, from_version: int, to_version: int
    ) -> None:
        """Callable seam matching ``init_db``'s ``Callable[[Connection, int, int], None]``.

        Required so a ``MigrationRunner`` can be passed directly as the
        ``migration_runner=`` argument of ``init_db()``. Implementations
        typically delegate to ``run_pending``.
        """
        ...


# ===========================================================================
# Layer 3 — notification plugin
# ===========================================================================


@runtime_checkable
class Notifier(Protocol):
    """Notification channel plugin interface (ADR-001).

    ``@runtime_checkable`` is needed so the ``ExplicitNotifierRegistry``
    can perform ``isinstance(obj, Notifier)`` guards when registering
    plugins and running tests.

    Class-level metadata (``ClassVar``) is required for auto-generated UI
    forms and for the ``with_retry`` decorator to forward channel identity.

    Send retry is NOT part of this interface — retry logic lives in
    ``NotifierDispatcher`` (services layer) which has access to
    ``NotificationsRepository`` for durable state (ADR-019).
    """

    channel_id: ClassVar[str]
    """Unique channel identifier: ``"email"``, ``"browser"``, ``"telegram"``, …"""

    display_name: ClassVar[str]
    """Human-readable channel name for UI display."""

    description: ClassVar[str]
    """Short human-readable description of the channel shown in the UI channel
    selector (e.g. ``"Send email notifications via SMTP"``). Used by the
    auto-generated channel-configuration form."""

    config_schema: ClassVar[type[NotifierConfig]]
    """Pydantic model class that describes per-channel configuration."""

    recipient_label: ClassVar[str]
    """UI label for the recipient field (e.g. ``"Email address"``)."""

    recipient_placeholder: ClassVar[str]
    """Placeholder text shown in the recipient input field
    (e.g. ``"user@example.com"``). Used by the auto-generated UI form to
    guide the user on the expected format."""

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        """Deliver a notification for ``lot`` to ``recipient``.

        Must be synchronous and thread-safe (called from the dispatcher
        thread). Returns ``NotifyResult`` — never raises for expected
        failures (network, auth). Unexpected programming errors may raise.
        """
        ...

    def test(self, recipient: str) -> NotifyResult:
        """Send a test message to ``recipient`` (no lot context).

        Used by ``POST /api/notifiers/{channel}/test``.
        """
        ...


