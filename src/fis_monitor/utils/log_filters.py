"""PII-scrubbing log filters for fis_monitor.

Provides:
- ``StackPIIFilter`` — a ``logging.Filter`` that scrubs URL query strings and
  session-token-like patterns from ``record.msg``, ``record.args``, and
  ``record.exc_text``.

Design decisions
----------------
- Filter returns ``True`` always: it mutates the record for scrubbing purposes
  but never rejects records.
- Idempotent: after the first scrub, the replacement markers ``?[scrubbed]``
  and ``[token-scrubbed]`` do not match the source regexes, so a second
  ``filter()`` call is a no-op.
- If any regex operation raises unexpectedly the filter catches the exception,
  emits a WARNING via ``"fis_monitor.log_filters._meta"`` (with
  ``propagate=False`` set explicitly, which breaks the path back to the
  fis_monitor handler — no recursion possible), and returns ``True``
  unchanged.
- Regex patterns are class-level constants so they can be imported and tested
  independently of a Logger / Handler setup.
- Constructor takes no required arguments (DI-friendly): callers pass an
  instance directly to ``setup_logging(filters=[StackPIIFilter()])``.

URL-query pattern rationale
---------------------------
We target only ``http(s)://`` URLs that contain a ``?`` query component, i.e.:

    https://api.example.com/path?token=ABC&other=XYZ

The regex captures:
  - scheme:  ``https?://``
  - host+path: one or more non-whitespace, non-? chars
  - literal ``?``
  - query string: one or more non-whitespace, non-closing-delimiter chars

Closing delimiters ``)``, ``]``, ``>``, ``"``, ``'`` and the opening bracket
``[`` are excluded from the query component so that: (a) URLs embedded in
parentheses, brackets, or quoted strings do not consume the surrounding
punctuation, and (b) the already-scrubbed marker ``?[scrubbed]`` does not
re-match on a second filter pass (idempotency guarantee).

Replacement: ``<scheme><host><path>?[scrubbed]``

This is deliberately conservative — a bare ``?`` in free text (e.g. "is it
ok?") does NOT match because there is no preceding URL scheme.

Token-scrubbing pattern rationale
----------------------------------
Session tokens, JWTs, API keys, and Bearer credentials share a structural
signature: long (≥ 24 char) base64/hex/alphanumeric runs with no whitespace.
The pattern is:

    [A-Za-z0-9+/=_-]{24,}

Accepts base64 alphabet (``+``, ``/``, ``=``), URL-safe base64 (``-``, ``_``),
and plain hex.  Minimum length 24 chars keeps ordinary English words and short
identifiers safe; tokens are almost always ≥ 32 chars.

False positives (e.g. a very long camelCase identifier) are *acceptable* — they
produce ``[token-scrubbed]`` in log output, which is safe.  False negatives
(a short token slipping through) are *unacceptable*.

The pattern is anchored by word-boundary lookarounds (``(?<![\\w])`` /
``(?![\\w])``) so that tokens embedded in longer words are also caught while
avoiding mid-word replacements in normal text.

See: [[utils/log#setup_logging]] (plg.1) for handler wiring.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

# ---------------------------------------------------------------------------
# Module-level "meta" logger — used only to warn about internal errors.
# propagate=False breaks the path back to the fis_monitor handler that this
# filter is attached to — no recursion possible even if _scrub_record raises.
# ---------------------------------------------------------------------------
_meta_log = logging.getLogger("fis_monitor.log_filters._meta")
_meta_log.propagate = False


class StackPIIFilter(logging.Filter):
    """Scrub URL query strings and session-token patterns from log records.

    Attaches to a ``logging.Handler`` via ``handler.addFilter(self)``.  The
    ``filter`` method mutates ``record.msg``, ``record.args``, and
    ``record.exc_text`` in-place, then returns ``True`` so the record is
    always emitted (just with sensitive data replaced).

    Usage::

        from fis_monitor.utils.log_filters import StackPIIFilter
        setup_logging(..., filters=[StackPIIFilter()])
    """

    # ------------------------------------------------------------------
    # Compiled regex patterns (class-level constants for direct testability)
    # ------------------------------------------------------------------

    #: Matches ``http(s)://host/path?query`` and captures everything up to
    #: the ``?`` in group 1 so we can reconstruct ``<url>?[scrubbed]``.
    #:
    #: Breakdown:
    #:   ``(https?://[^\s?]+)``         — scheme + host + path (no spaces, no ``?``)
    #:   ``\?``                          — literal query separator
    #:   ``[^\s)\]>\"'\[]+``            — one or more non-whitespace, non-closing-
    #:                                     delimiter query chars; excludes ``)``,
    #:                                     ``]``, ``[``, ``>``, ``"``, ``'`` so that:
    #:                                     (a) a URL inside parens/brackets/quotes
    #:                                     does not consume the surrounding
    #:                                     punctuation, and (b) the already-scrubbed
    #:                                     marker ``?[scrubbed]`` does not re-match
    #:                                     on a second filter pass (idempotency).
    URL_QUERY_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"""(https?://[^\s?]+)\?[^\s)\]>\"'\[]+"""
    )

    #: Matches token-like strings: ≥ 24 chars from the base64/hex alphabet,
    #: not adjacent to other word characters (prevents mid-word replacement).
    #:
    #: Alphabet: ``A-Za-z0-9`` + ``+/=`` (base64) + ``-_`` (URL-safe base64 / JWT).
    #: Length gate: ``{24,}`` — anything shorter is assumed to be ordinary text.
    TOKEN_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{24,}(?![A-Za-z0-9+/=_-])"
    )

    # Replacement literals — also used by tests as reference values.
    _URL_SCRUB_REPL: ClassVar[str] = r"\1?[scrubbed]"
    _TOKEN_SCRUB_REPL: ClassVar[str] = "[token-scrubbed]"

    # ------------------------------------------------------------------
    # Private helpers — unit-testable independently of the filter() method
    # ------------------------------------------------------------------

    @classmethod
    def _scrub_url(cls, text: str) -> str:
        """Replace query strings in URL-like substrings with ``?[scrubbed]``."""
        return cls.URL_QUERY_RE.sub(cls._URL_SCRUB_REPL, text)

    @classmethod
    def _scrub_tokens(cls, text: str) -> str:
        """Replace token-like alphanumeric strings with ``[token-scrubbed]``."""
        return cls.TOKEN_RE.sub(cls._TOKEN_SCRUB_REPL, text)

    @classmethod
    def _scrub(cls, text: str) -> str:
        """Apply both scrubbing passes in sequence."""
        text = cls._scrub_url(text)
        text = cls._scrub_tokens(text)
        return text

    # ------------------------------------------------------------------
    # logging.Filter API
    # ------------------------------------------------------------------

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub PII from *record* in-place; always returns ``True``.

        Mutates:
        - ``record.msg`` — the format string / plain message.
        - ``record.args`` — positional format args (strings only; non-strings
          are left unchanged to avoid breaking ``%``-style formatting).
        - ``record.exc_text`` — cached traceback string (if set).
        """
        try:
            self._scrub_record(record)
        except Exception:
            _meta_log.warning(
                "StackPIIFilter: unexpected error while scrubbing record; "
                "record emitted without scrubbing.",
                exc_info=True,
                stacklevel=1,
            )
        return True

    # ------------------------------------------------------------------
    # Internal implementation — separated for clarity and testability
    # ------------------------------------------------------------------

    def _scrub_record(self, record: logging.LogRecord) -> None:
        """Apply scrubbing to all text fields of *record* in-place."""
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a
                    for a in record.args
                )

        if record.exc_text is not None:
            record.exc_text = self._scrub(record.exc_text)
