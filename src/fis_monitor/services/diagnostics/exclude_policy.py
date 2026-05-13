"""DiagnosticsExcludePolicy — PII exclude/redact rules for diagnostic.zip.

PII MUST NOT leak through diagnostic dumps.  ``audit.jsonl`` is the only
legal channel for PII data (see docs/decisions/ADR-012-diagnostic-zip-allowlist-redactor.md,
docs/decisions/ADR-017-secrets-secretstr-crash-dump-exclusion.md).

Design principles
-----------------
* **SRP** — this module only *defines* what to hide; it does not build the zip
  (that is DiagnosticsService's job, a4t.7).
* **Fail-closed** by intent: DiagnosticsService should default-redact unknown
  fields; this policy only covers the known PII surface.
* **Pure functions** — no I/O, no state mutations.  Every method returns a
  fresh dict, making the policy trivially testable in isolation.
* **Explicit SSOT** — the frozen sets are the single source of truth for PII
  field coverage.  Adding a new sensitive field requires a deliberate edit
  here *and* a test update.

Redaction patterns
------------------
``redact_error`` replaces the following patterns with a ``[REDACTED]``
placeholder (order matters — URLs before emails to avoid double-matching):

1. ``https?://...`` — full URLs (path, query, fragment included).
2. ``[\\w.+\\-]+@[\\w.\\-]+\\.[a-z]{2,}`` — RFC-5321-ish email addresses.
3. Relative traversal paths ``../../...`` — directory traversal sequences.
4. Unix absolute paths  ``/...`` (non-whitespace run starting with ``/``).
5. Windows absolute paths ``C:\\...`` (drive-letter + colon + backslash,
   capturing space-separated path segments until a double-space, end of line,
   or a hard delimiter ``,:;``).

Patterns are compiled once at module load time (``_REDACT_PATTERNS``).

Limitations
-----------
* Email TLD matching uses ``[a-z]{2,}`` — ASCII TLDs only; internationalised
  domain names (IDN) are not covered.
* ``redact_error`` is best-effort for *string* values.  Non-string ``error``
  column values (legacy migration artifacts) are returned unchanged — see
  ``filter_row`` guard.
* URLs inside JSON payloads embedded in error strings are matched by the URL
  pattern only if the JSON is serialised inline (schema-required / structured
  log formats may need a separate sanitiser).
"""

from __future__ import annotations

import copy
import re
from typing import ClassVar, Final

# ---------------------------------------------------------------------------
# Pre-compiled redaction patterns (compiled once, shared across all calls)
# ---------------------------------------------------------------------------

# Order matters:
#   1. URLs first (they contain '/' which would also match the Unix path RE)
#   2. Emails before paths (email local-part can contain slashes in exotic cases)
#   3. Relative traversal before absolute Unix path (both start with non-space)
#   4. Unix absolute path
#   5. Windows absolute path
_REDACT_PATTERNS: Final[list[re.Pattern[str]]] = [
    # https?:// URL — greedy non-whitespace run after the scheme+host
    re.compile(r"https?://\S+", re.IGNORECASE),
    # email address
    re.compile(r"[\w.+\-]+@[\w.\-]+\.[a-z]{2,}", re.IGNORECASE),
    # Relative traversal: one or more "../" sequences followed by a path
    re.compile(r"(?:\.\./)+\S+"),
    # Unix absolute path: starts with / followed by at least one non-space char
    re.compile(r"/\S+"),
    # Windows absolute path: drive letter, colon, backslash.
    # Captures space-separated path segments (e.g. "C:\Program Files\app\log.txt")
    # and stops before double-space, end-of-line, or hard delimiters (,:;).
    re.compile(r"[A-Za-z]:\\\S+(?:[ ]\S+)*"),
]

_PLACEHOLDER: Final[str] = "[REDACTED]"


class DiagnosticsExcludePolicy:
    """Determines which fields to exclude/redact when building diagnostic.zip.

    PII MUST NOT leak through diagnostic dumps. ``audit.jsonl`` is the only
    legal channel for PII data.

    Usage::

        policy = DiagnosticsExcludePolicy()
        safe_settings = policy.filter_settings(raw_settings)
        safe_row = policy.filter_row("cycles", db_row)
        safe_state_rows = policy.filter_state_keys(raw_state_rows)
    """

    # ------------------------------------------------------------------
    # Configuration-tree paths to exclude (dotted notation)
    # ------------------------------------------------------------------
    EXCLUDED_SETTINGS_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "notifications.email.recipients",
            "notifications.email.from_address",
        }
    )

    # ------------------------------------------------------------------
    # Database table/field pairs to exclude entirely
    # ------------------------------------------------------------------
    EXCLUDED_DB_FIELDS: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {
            ("lot_user_state", "note"),
            ("notifications", "recipient"),  # direct PII: email / identifier
        }
    )

    # ------------------------------------------------------------------
    # Database table/field pairs to redact (URL / email / path patterns)
    # ------------------------------------------------------------------
    REDACTED_DB_FIELDS: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {
            ("cycles", "error"),
        }
    )

    # ------------------------------------------------------------------
    # State-table key allowlist (fail-closed: unknown keys are dropped)
    # ------------------------------------------------------------------
    STATE_ALLOWED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "monitor_paused",
            "last_full_scan_at",
            "onboarded",
            "onboarding_step",
        }
    )

    # Defence-in-depth: even if a key is in STATE_ALLOWED_KEYS, drop it if
    # its name contains any of these substrings (case-insensitive).
    STATE_FORBIDDEN_SUBSTRINGS: ClassVar[frozenset[str]] = frozenset(
        {"password", "secret", "token"}
    )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def filter_settings(self, settings: dict) -> dict:  # type: ignore[type-arg]
        """Return a deep copy of *settings* with all EXCLUDED_SETTINGS_PATHS removed.

        Traversal is driven by ``EXCLUDED_SETTINGS_PATHS`` (dotted strings).
        Missing intermediate keys are silently ignored — the caller is not
        required to supply a complete settings dict.  Non-dict intermediate
        values (e.g. ``{"notifications": "disabled"}``) are also silently
        skipped and left unchanged.

        Args:
            settings: Raw settings dictionary (any depth).

        Returns:
            A new ``dict`` with PII paths removed.  The original is not mutated.
        """
        result = copy.deepcopy(settings)
        for dotted_path in self.EXCLUDED_SETTINGS_PATHS:
            _delete_dotted(result, dotted_path.split("."))
        return result

    def filter_row(self, table: str, row: dict) -> dict:  # type: ignore[type-arg]
        """Return a sanitised copy of *row* for *table*.

        * Fields in ``EXCLUDED_DB_FIELDS`` for this table are dropped.
        * Fields in ``REDACTED_DB_FIELDS`` for this table have their string
          value passed through ``redact_error``; non-string values (including
          ``None`` and legacy integer artifacts) are kept as-is without
          redaction to avoid ``TypeError``.
        * All other fields pass through unchanged.

        Args:
            table: Database table name (e.g. ``"cycles"``).
            row:   Mapping of column names to values.

        Returns:
            A new ``dict``.  The original *row* is not mutated.
        """
        result: dict = {}  # type: ignore[type-arg]
        for field, value in row.items():
            if (table, field) in self.EXCLUDED_DB_FIELDS:
                continue  # drop entirely
            if (table, field) in self.REDACTED_DB_FIELDS:
                # Guard: only redact actual strings — non-string values pass through
                result[field] = self.redact_error(value) if isinstance(value, str) else value
            else:
                result[field] = value
        return result

    def filter_state_keys(self, state_rows: list[dict]) -> list[dict]:  # type: ignore[type-arg]
        """Apply state-table policy: keep only ``STATE_ALLOWED_KEYS``.

        The ``state`` table stores arbitrary key/value pairs; only a small
        known-safe subset should appear in a diagnostic dump.  Unknown keys
        are dropped (fail-closed).  As a defence-in-depth measure, keys
        containing ``STATE_FORBIDDEN_SUBSTRINGS`` are also dropped even if
        they somehow appear in the allowlist.

        Args:
            state_rows: List of row dicts, each expected to have a ``"key"``
                field (the state-table primary key).  Rows without a ``"key"``
                field are dropped.

        Returns:
            A new list containing only the allowed rows.  Original dicts are
            not mutated.
        """
        result = []
        for row in state_rows:
            key = row.get("key", "")
            if key not in self.STATE_ALLOWED_KEYS:
                continue
            key_lower = key.lower()
            if any(s in key_lower for s in self.STATE_FORBIDDEN_SUBSTRINGS):
                continue
            result.append(dict(row))
        return result

    @staticmethod
    def redact_error(text: str | None) -> str | None:
        """Replace URLs, email addresses, and file-system paths with ``[REDACTED]``.

        This is a best-effort defence: it handles the most common PII vectors
        found in ``cycles.error`` (HTTP URLs from scraping failures, email
        addresses in SMTP errors, file paths in stack traces, directory
        traversal sequences).

        Args:
            text: Raw error string, or ``None``.

        Returns:
            Redacted string, or ``None`` if *text* is ``None``.

        Limitations:
            * TLD matching is ASCII-only (``[a-z]{2,}``); IDN TLDs are missed.
            * Windows paths with spaces are matched greedily across
              single-space boundaries; a trailing word after a double-space
              separator will not be captured (acceptable trade-off).
        """
        if text is None:
            return None
        for pattern in _REDACT_PATTERNS:
            text = pattern.sub(_PLACEHOLDER, text)
        return text


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _delete_dotted(d: dict, parts: list[str]) -> None:  # type: ignore[type-arg]
    """Recursively traverse *d* following *parts* and delete the final key.

    No-op if any intermediate key is missing or is not a ``dict``.
    """
    if not parts:
        return
    key, *rest = parts
    if key not in d:
        return
    if not rest:
        del d[key]
    elif isinstance(d[key], dict):
        _delete_dotted(d[key], rest)
