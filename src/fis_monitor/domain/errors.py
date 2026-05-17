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

from typing import Literal


class DomainError(Exception):
    """Root of the domain exception tree.

    PII rule
    --------
    Exception args (the *args/message passed to raise) MUST NOT contain
    personally identifiable or sensitive data: URLs with query params or
    auth tokens, cadastral_no, email addresses, recipient identifiers,
    SMTP credentials, lot detail bodies. logging.exception propagates
    args verbatim into audit.jsonl — any PII here leaks downstream.
    Use safe categorical detail ("auth_failed", "parse_error"); attach
    identifiers via structured log fields, never via the exception message.
    """


class UpstreamError(DomainError):
    """Failure originating from an external system (HTTP, DNS, SMTP, …).

    Attributes:
        category: Closed enum of upstream failure types per
                  docs/architecture/08-error-strategy.md.
    """

    UpstreamCategory = Literal[
        "network", "http_5xx", "http_4xx", "redirect_login", "timeout"
    ]

    def __init__(self, message: str, *, category: UpstreamError.UpstreamCategory) -> None:
        super().__init__(message)
        self.category = category


class SmtpHostPolicyError(UpstreamError):
    """SMTP host failed policy validation (DNS-rebinding, blocklist, bad TLD, …).

    Message includes the host name and the reason category (e.g. "private IP",
    "loopback", "cloud metadata endpoint").  Do NOT include the resolved IP
    address or any recipient-derived data — those are PII-ish audit-only fields.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, category="network")


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

    See DomainError PII rule.

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
    """Stored `raw_json` schema version ≠ current parser version → lazy reparse.

    See DomainError PII rule.
    """


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


class RegistrationError(DomainError):
    """Raised by ``NotifierRegistry.register()`` on duplicate channel_id or
    when the supplied object does not satisfy the ``Notifier`` Protocol.

    PII contract: message contains only the channel_id string — no addresses,
    credentials, or recipient data.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidTransitionError(DomainError):
    """Raised by ``OnboardingService.advance()`` when the requested transition
    is illegal — either the current state differs from ``from_state`` or the
    guard for ``from_state → to_state`` is not satisfied.

    Attributes:
        current_state: the actual current state in the DB at advance-time.
        requested_from: the ``from_state`` argument the caller passed.
        requested_to: the ``to_state`` argument the caller passed.
    """

    def __init__(
        self,
        current_state: str,
        requested_from: str,
        requested_to: str,
    ) -> None:
        super().__init__(
            f"Invalid onboarding transition: current={current_state}, "
            f"requested={requested_from}→{requested_to}"
        )
        self.current_state = current_state
        self.requested_from = requested_from
        self.requested_to = requested_to


class SessionExpiredError(DomainError):
    """HTTP response indicates the session is expired — site redirected to ESIA login.

    Raised by ``SelectolaxListParser.parse()`` when the HTML response contains
    ESIA (esia.gosuslugi.ru) redirect markers instead of the expected lot-list DOM.

    This is NOT a ``ParseBugError`` — the DOM is not broken; the session cookie
    is expired and the site returned a login page.  Callers (``MonitorCycleService``,
    ``FullScanService``) should log a WARN and trigger re-authentication rather than
    treating this as a site-DOM change.

    PII contract: message and attributes contain NO session tokens, cookies,
    URLs with query params, or user-identifying data.
    """


class SmtpStarttlsError(UpstreamError):
    """Raised by ``SmtpEmailNotifier.send()`` when the STARTTLS handshake
    returns a non-220 SMTP reply code.

    Surfaced as ``NotifyResult(ok=False, retryable=True)`` by the notifier;
    not propagated to the cycle. PII contract: message contains only the
    integer SMTP reply code — no host, recipient, or password material.
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"STARTTLS rejected by server (code={code})", category="network")
        self.code = code
