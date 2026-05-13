"""DiagnosticsExcludePolicy — PII exclude/redact rules for diagnostic.zip.

PII MUST NOT leak through diagnostic dumps.  ``audit.jsonl`` is the only
legal channel for PII data (see decisions-log ADR-012, ADR-017).

Design principles
-----------------
* **SRP** — this module only *defines* what to hide; it does not build the zip
  (that is DiagnosticsService's job, a4t.7).
* **Fail-closed** by intent: DiagnosticsService should default-redact unknown
  fields; this policy only covers the known PII surface.
* **Pure functions** — no I/O, no state mutations.  Every method returns a
  fresh dict, making the policy trivially testable in isolation.
* **Explicit SSOT** — the three frozen sets are the single source of truth
  for PII field coverage.  Adding a new sensitive field requires a deliberate
  edit here *and* a test update.

Redaction patterns
------------------
``redact_error`` replaces the following patterns with a ``[REDACTED]``
placeholder (order matters — URLs before emails to avoid double-matching):

1. ``https?://...`` — full URLs (path, query, fragment included).
2. ``[\\w.+\\-]+@[\\w.\\-]+\\.[a-z]{2,}`` — RFC-5321-ish email addresses.
3. Unix absolute paths  ``/...`` (non-whitespace run starting with ``/``).
4. Windows absolute paths ``C:\\...`` (drive-letter + colon + backslash run).

Patterns are compiled once at module load time (``_REDACT_PATTERNS``).
"""

from __future__ import annotations

import copy
import re
from typing import Final

# ---------------------------------------------------------------------------
# Pre-compiled redaction patterns (compiled once, shared across all calls)
# ---------------------------------------------------------------------------

# Order matters: URLs first (they contain '/' which would also match path RE),
# then emails, then file-system paths.
_REDACT_PATTERNS: Final[list[re.Pattern[str]]] = [
    # https?:// URL — greedy non-whitespace run after the scheme+host
    re.compile(r"https?://\S+", re.IGNORECASE),
    # email address
    re.compile(r"[\w.+\-]+@[\w.\-]+\.[a-z]{2,}", re.IGNORECASE),
    # Unix absolute path: starts with / followed by at least one non-space char
    re.compile(r"/\S+"),
    # Windows absolute path: drive letter, colon, backslash, then non-space chars
    re.compile(r"[A-Za-z]:\\[^\s]+"),
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
    """

    # ------------------------------------------------------------------
    # Configuration-tree paths to exclude (dotted notation)
    # ------------------------------------------------------------------
    EXCLUDED_SETTINGS_PATHS: frozenset[str] = frozenset(
        {
            "notifications.email.recipients",
            "notifications.email.from_address",
        }
    )

    # ------------------------------------------------------------------
    # Database table/field pairs to exclude entirely
    # ------------------------------------------------------------------
    EXCLUDED_DB_FIELDS: frozenset[tuple[str, str]] = frozenset(
        {
            ("lot_user_state", "note"),
        }
    )

    # ------------------------------------------------------------------
    # Database table/field pairs to redact (URL / email / path patterns)
    # ------------------------------------------------------------------
    REDACTED_DB_FIELDS: frozenset[tuple[str, str]] = frozenset(
        {
            ("cycles", "error"),
        }
    )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def filter_settings(self, settings: dict) -> dict:  # type: ignore[type-arg]
        """Return a deep copy of *settings* with all EXCLUDED_SETTINGS_PATHS removed.

        Traversal is driven by ``EXCLUDED_SETTINGS_PATHS`` (dotted strings).
        Missing intermediate keys are silently ignored — the caller is not
        required to supply a complete settings dict.

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
          value passed through ``redact_error``; ``None`` values stay ``None``.
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
                result[field] = self.redact_error(value)
            else:
                result[field] = value
        return result

    @staticmethod
    def redact_error(text: str | None) -> str | None:
        """Replace URLs, email addresses, and file-system paths with ``[REDACTED]``.

        This is a best-effort defence: it handles the most common PII vectors
        found in ``cycles.error`` (HTTP URLs from scraping failures, email
        addresses in SMTP errors, file paths in stack traces).

        Args:
            text: Raw error string, or ``None``.

        Returns:
            Redacted string, or ``None`` if *text* is ``None``.
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

    No-op if any intermediate key is missing.
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
