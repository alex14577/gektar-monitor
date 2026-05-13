"""Domain exception hierarchy.

All domain errors derive from `DomainError`. `ParseBugError` and
`ParserVersionMismatch` are **siblings** (not in a subclass relationship)
because they trigger different recovery paths:

* `ParseBugError`     — site DOM shape changed unexpectedly → cycle.error event.
* `ParserVersionMismatch` — stored `raw_json` was produced by an older parser
  version → lazy reparse (no user-facing alert).

Mixing the two via inheritance would break `except` semantics in
`MonitorCycleService` (R3-ADR — see docs/decisions-log.md MOC).
"""

from __future__ import annotations


class DomainError(Exception):
    """Root of the domain exception tree.

    Do NOT include PII in exception args (URLs with query params, cadastral_no,
    emails, recipient addresses) — they propagate to `logging.exception` →
    `audit.jsonl`. See bd issue `gektar_monitor-4kh` for the redaction
    helper / lint rule follow-up.
    """


class UpstreamError(DomainError):
    """Failure originating from an external system (HTTP, DNS, SMTP, …)."""


class SmtpHostPolicyError(UpstreamError):
    """SMTP host failed policy validation (DNS-rebinding, blocklist, bad TLD, …).

    Message includes the host name and the reason category (e.g. "private IP",
    "loopback", "cloud metadata endpoint").  Do NOT include the resolved IP
    address or any recipient-derived data — those are PII-ish audit-only fields.
    """


class MigrationRequired(DomainError):
    """Database schema version is older than the application expects.

    Raised by ``init_db()`` when ``PRAGMA user_version < latest_version`` and
    no ``migration_runner`` is provided.  The caller (composition root) is
    responsible for either running the migration or surfacing a human-readable
    message.

    Attributes:
        from_version: The ``user_version`` found in the database.
        to_version:   The ``latest_version`` the application requires.

    PII contract: message and attributes contain ONLY version integers — no
    file paths, no database path, no user data.
    """

    def __init__(self, from_version: int, to_version: int) -> None:
        super().__init__(
            f"Database schema migration required: version {from_version} → {to_version}"
        )
        self.from_version = from_version
        self.to_version = to_version


class ConcurrentMigrationError(DomainError):
    """Database `user_version` changed between init_db read and runner BEGIN IMMEDIATE.

    Raised by ``SqliteMigrationRunner.run_pending`` after acquiring the writer
    lock when ``PRAGMA user_version`` no longer equals the expected
    ``from_version``.  Indicates a concurrent migration (another process /
    worker) — defence-in-depth even when single-instance lock is in effect
    (see bd issue ``1zk``).

    PII contract: only version integers.
    """

    def __init__(self, expected_version: int, actual_version: int) -> None:
        super().__init__(
            f"Concurrent migration detected: expected user_version "
            f"{expected_version}, found {actual_version}"
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class MigrationChainBroken(DomainError):
    """No continuous migration chain from `from_version` to `to_version`.

    Raised by ``SqliteMigrationRunner.run_pending`` when the registered
    migrations cannot reach ``to_version`` from ``from_version`` by chaining
    contiguous steps.  Configuration error — never recoverable at runtime.

    PII contract: only version integers.
    """

    def __init__(self, from_version: int, to_version: int) -> None:
        super().__init__(
            f"No migration chain from version {from_version} to {to_version}"
        )
        self.from_version = from_version
        self.to_version = to_version


class BusyError(DomainError):
    """Single-flight conflict: a ``LoginSession.open_headed_login`` call is
    already in progress.

    Raised by ``PlaywrightLoginSession.open_headed_login`` when the internal
    ``threading.Lock`` cannot be acquired (``blocking=False``), meaning another
    caller is already running a headed-login workflow.

    Callers should surface this to the user as "login already in progress"
    and NOT retry immediately.
    """


class ParseBugError(DomainError):
    """DOM-shape сломан — селектор не нашёл ожидаемого узла.

    raise → cycle.error event. ``selector`` и ``context`` — короткие
    PII-safe строки (никаких raw HTML фрагментов, recipient, email).

    Attributes:
        selector: CSS selector or field name that failed to match.
        context:  Short hint about the page/position (PII-free).
    """

    def __init__(self, selector: str, context: str = "") -> None:
        """Initialise ParseBugError with a CSS selector and optional context hint.

        Args:
            selector: The CSS selector or field name that failed to match.
                      Must be a short, PII-safe string (e.g. ``"tbody.lots-list"``,
                      ``"date_update"``).
            context:  Optional short hint about the page/position context.
                      Must be a short, PII-safe string (e.g. ``"empty rows"``,
                      ``"missing main block"``).

        **PROHIBITED in ``context`` (and ``selector``) — PII-safety is a
        caller-responsibility convention, NOT enforced at construction time:**

        * Raw HTML fragments (e.g. ``"<div class=...">``)
        * URLs or query parameters (e.g. ``"https://…?id=123"``)
        * Email addresses (e.g. ``"user@example.com"``)
        * User-supplied input (cadastral numbers, recipient names, etc.)

        These fields propagate to ``logging.exception`` and ``audit.jsonl``.
        The constructor will accept any string — enforcement is the caller's
        responsibility.
        """
        self.selector = selector
        self.context = context
        msg = f"parse bug: selector={selector!r}"
        if context:
            msg += f" context={context!r}"
        super().__init__(msg)


class ParserVersionMismatch(DomainError):
    """Stored `raw_json` schema version ≠ current parser version → lazy reparse."""


class AlreadyRunningError(DomainError):
    """Another instance holds the OS-level lock.

    Raised by ``Locker.acquire()`` when the lock-file is already locked by
    another instance. `holder_pid` is the PID stamped in the lock-file for
    diagnostics (info-only, not used for arbitration). If the holder process
    has exited, the OS-lock is not held and a fresh acquire will succeed.

    Attributes:
        holder_pid: The PID of the process holding the lock, or None if
                    the lock-file exists but the PID cannot be read.

    PII contract: message contains only the holder PID integer — no paths,
    no timestamps, no user data.
    """

    def __init__(self, holder_pid: int | None = None) -> None:
        msg = "Another instance is already running"
        if holder_pid is not None:
            msg += f" (PID {holder_pid})"
        super().__init__(msg)
        self.holder_pid = holder_pid
