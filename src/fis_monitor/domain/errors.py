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


class ParseBugError(DomainError):
    """HTML/JSON shape diverged from parser expectations — unrecoverable cycle bug."""


class ParserVersionMismatch(DomainError):
    """Stored `raw_json` schema version ≠ current parser version → lazy reparse."""
