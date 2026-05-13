"""Domain exception hierarchy.

All domain errors derive from `DomainError`. `ParseBugError` and
`ParserVersionMismatch` are **siblings** (not in a subclass relationship)
because they trigger different recovery paths:

* `ParseBugError`     — site DOM shape changed unexpectedly → cycle.error event.
* `ParserVersionMismatch` — stored `raw_json` was produced by an older parser
  version → lazy reparse (no user-facing alert).

Mixing the two via inheritance would break `except` semantics in
`MonitorCycleService` (R3-ADR — see decisions-log).
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


class ParseBugError(DomainError):
    """HTML/JSON shape diverged from parser expectations — unrecoverable cycle bug."""


class ParserVersionMismatch(DomainError):
    """Stored `raw_json` schema version ≠ current parser version → lazy reparse."""
