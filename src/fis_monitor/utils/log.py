"""Structured JSON logging utilities for fis_monitor.

Provides:
- ``JsonFormatter`` — formats log records as single-line JSON (Clock-injected timestamps).
- ``setup_logging`` — idempotent handler registration on the ``fis_monitor`` logger.
- ``get_logger`` — thin alias over ``logging.getLogger`` for consistent naming.
- ``log_request`` — whitelist-only HTTP access log helper (requests channel).

Three file channels (enabled when ``data_dir`` is set in ``setup_logging``):
- ``<data_dir>/logs/audit.jsonl``    — config-diff write-only (PII allowed, NO
  StackPIIFilter); fail-closed: unwritable dir → single WARNING, then silent no-op.
- ``<data_dir>/logs/app.jsonl``      — all info+ structured logs via StackPIIFilter.
- ``<data_dir>/logs/requests.jsonl`` — whitelist-only HTTP access logs written by
  ``log_request()``.

Design decisions
----------------
- Handler is registered on the ``"fis_monitor"`` logger, **not** the root logger.
  ``propagate=True`` is kept so pytest's ``caplog`` fixture (which attaches to
  the root logger) can observe records.  No double-output in production:
  uvicorn registers its handlers on the ``"uvicorn"`` / ``"uvicorn.access"``
  loggers (with ``propagate=False``), not on root.
- ``setup_logging`` is idempotent: a sentinel attribute guards against adding
  duplicate handlers across multiple calls (e.g. bootstrap + lifespan startup).
- ``JsonFormatter`` accepts a ``Clock`` dependency for testable UTC timestamps.
- All ``extra={...}`` keys are namespaced under ``"ctx"`` — reserved top-level
  keys (timestamp/level/logger/message/service/trace_id) cannot be overridden.
- ``exc_info`` is serialised to full traceback string under ``"exc"`` key.
- Non-primitive ``ctx`` values are handled by ``_json_default``:
  Pydantic ``BaseModel`` → ``model_dump()``, ``datetime`` → ``.isoformat()``,
  everything else → ``str()``.
- File channels use ``TimedRotatingFileHandler(when="midnight", backupCount=30,
  utc=True)`` — daily rotation, 30-day retention, UTC rollover time.
- Audit channel: separate ``"fis_monitor.audit"`` logger, no propagation to
  parent (PII expected; parent has StackPIIFilter).  Fail-closed on unwritable
  ``logs/`` dir — single WARNING on ``fis_monitor``, silent no-op thereafter.
- Requests channel: separate ``"fis_monitor.requests"`` logger; written via
  ``log_request()`` helper which enforces the field whitelist and strips URL
  query strings.

See: docs/architecture/02-layers-dip.md for layer placement rationale.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import traceback
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import Clock

# ---------------------------------------------------------------------------
# Sentinel attribute names placed on loggers/handlers to detect
# whether our handlers have already been installed (idempotency).
# ---------------------------------------------------------------------------
_SENTINEL = "_fis_monitor_json_handler_installed"
_FILE_SENTINEL_PREFIX = "_fis_monitor_file_handler_"

# Top-level keys that cannot be overridden by caller-supplied extras.
_RESERVED_KEYS = frozenset(
    {"timestamp", "level", "logger", "message", "service", "trace_id", "exc"}
)

_FIS_MONITOR_LOGGER = "fis_monitor"
_AUDIT_LOGGER = "fis_monitor.audit"
_REQUESTS_LOGGER = "fis_monitor.requests"

# Whitelist of fields allowed in requests.jsonl (canon: 10-9-http-logs.md).
_REQUEST_WHITELIST_FIELDS = frozenset(
    {"timestamp", "level", "logger", "message", "service", "trace_id",
     "method", "url_path", "status", "duration_ms", "bytes", "parser_version"}
)


# ---------------------------------------------------------------------------
# JSON default serialiser for non-primitive types
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """Fallback serialiser for ``json.dumps``.

    Priority:
    1. Pydantic ``BaseModel`` → ``model_dump()`` (no ``mode='json'``).
       When fields are correctly declared as ``SecretStr``/``SecretBytes``,
       Pydantic serialises them as ``'**********'`` (mask repr) — plaintext
       is never exposed.  WARNING: secrets stored in plain ``str`` fields are
       NOT masked — that is the model's responsibility.  PII in ordinary
       fields must be scrubbed by the plg.2 redactor pipeline.
       # TODO(sec): once plg.2 redactor is available, add exclude=True
       # for fields tagged as PII via field metadata.
    2. ``datetime`` → ``.isoformat()``.
    3. Everything else → ``str(obj)``.
    """
    try:
        # Pydantic v2 BaseModel — import only when needed.
        from pydantic import BaseModel

        if isinstance(obj, BaseModel):
            return obj.model_dump()
    except ImportError:
        pass

    if isinstance(obj, datetime):
        return obj.isoformat()

    return str(obj)


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Formats a ``LogRecord`` as a single-line JSON string.

    Args:
        clock: ``Clock`` Protocol implementation (``SystemClock`` in production,
               ``FakeClock`` in tests). Used for the ``timestamp`` field.
    """

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self._clock = clock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ctx(self, record: logging.LogRecord) -> dict[str, Any]:
        """Extract caller-supplied extras into the ``ctx`` dict.

        Skips LogRecord built-in attributes and our reserved top-level keys.
        """
        # Built-in LogRecord attributes to exclude from extras.
        _builtin = frozenset(
            {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
                "taskName",
            }
        )
        ctx: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _builtin or key in _RESERVED_KEYS:
                continue
            ctx[key] = value
        return ctx

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a compact JSON string."""
        # Ensure record.message is populated.
        record.getMessage()

        payload: dict[str, Any] = {
            "timestamp": self._clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": _FIS_MONITOR_LOGGER,
            "trace_id": None,  # populated by plg.3 via contextvars
        }

        ctx = self._build_ctx(record)
        if ctx:
            payload["ctx"] = ctx

        if record.exc_info:
            exc_lines = traceback.format_exception(*record.exc_info)
            payload["exc"] = "".join(exc_lines)
        elif record.exc_text:
            payload["exc"] = record.exc_text

        return json.dumps(payload, default=_json_default, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plain (human-readable) formatter for LOG_JSON=0 mode
# ---------------------------------------------------------------------------

_PLAIN_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"


# ---------------------------------------------------------------------------
# Internal helpers — file channel construction
# ---------------------------------------------------------------------------


def _make_rotating_handler(
    path: Path,
    clock: Clock,
    *,
    level: int = logging.INFO,
) -> logging.handlers.TimedRotatingFileHandler:
    """Create a UTC midnight-rotating JSONL file handler."""
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        backupCount=30,
        utc=True,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter(clock=clock))
    handler.setLevel(level)
    return handler


def _remove_file_handlers(logger: logging.Logger, channel: str) -> None:
    """Remove previously-installed file handlers for *channel* from *logger*."""
    sentinel = _FILE_SENTINEL_PREFIX + channel
    for handler in list(logger.handlers):
        if getattr(handler, sentinel, False):
            logger.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def setup_logging(
    *,
    clock: Clock,
    stream: TextIO = sys.stdout,
    level: int = logging.INFO,
    json_format: bool = True,
    filters: Sequence[logging.Filter] | None = None,
    data_dir: Path | None = None,
) -> None:
    """Install (or replace) a handler on the ``fis_monitor`` logger.

    Idempotent: if our handler is already installed, the existing handler is
    **replaced** (so a second call with different ``stream``/``level`` takes
    effect, e.g. bootstrap stderr → lifespan stdout).  This avoids duplicate
    output while still allowing reconfiguration.

    When ``data_dir`` is provided, three rotating file channels are also
    installed (or replaced):

    - ``<data_dir>/logs/app.jsonl``      — mirrors all fis_monitor records,
      with the same ``filters`` as the console handler (StackPIIFilter).
    - ``<data_dir>/logs/audit.jsonl``    — written via ``"fis_monitor.audit"``
      logger; NO StackPIIFilter (PII expected); fail-closed on unwritable dir.
    - ``<data_dir>/logs/requests.jsonl`` — written via ``"fis_monitor.requests"``
      logger; whitelist-only fields via ``log_request()`` helper.

    When ``data_dir`` is ``None``, file channels are disabled (existing
    console-only behaviour for tests/bootstrap).

    Args:
        clock:       ``Clock`` for JSON timestamp generation.
        stream:      Output stream (``sys.stdout`` in production,
                     ``sys.stderr`` for bootstrap, ``io.StringIO`` in tests).
        level:       Logging level (``logging.INFO`` default).
        json_format: ``True`` → ``JsonFormatter``; ``False`` → plain text.
        filters:     Optional sequence of ``logging.Filter`` instances to attach
                     to the handler.  Each filter is added via
                     ``handler.addFilter(f)``.  Filters are applied in order.
                     Pass ``[StackPIIFilter()]`` for PII scrubbing.
        data_dir:    When set, create ``logs/`` subdir and install three
                     rotating JSONL file channels.  ``None`` → file channels
                     disabled (console-only mode).
    """
    root_logger = logging.getLogger(_FIS_MONITOR_LOGGER)

    # Remove any previously-installed console handler of ours (idempotency).
    for handler in list(root_logger.handlers):
        if getattr(handler, _SENTINEL, False):
            root_logger.removeHandler(handler)
            handler.close()

    # Build the new console handler.
    handler = logging.StreamHandler(stream)
    if json_format:
        handler.setFormatter(JsonFormatter(clock=clock))
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT))
    handler.setLevel(level)

    # Mark handler as ours so future calls can identify and replace it.
    setattr(handler, _SENTINEL, True)

    # Attach caller-supplied filters (e.g. StackPIIFilter for PII scrubbing).
    for f in filters or []:
        handler.addFilter(f)

    root_logger.setLevel(level)
    # propagate=True: records can still reach root (e.g. pytest caplog fixture).
    # Duplication risk is minimal: uvicorn installs its handlers on the "uvicorn"
    # and "uvicorn.access" loggers, not root, so fis_monitor records do not
    # trigger a second output path in production.
    root_logger.propagate = True
    root_logger.addHandler(handler)

    # ------------------------------------------------------------------
    # File channels — only when data_dir is provided.
    # Always clean up previously-installed file handlers on child loggers
    # so that a data_dir=None call after a data_dir=<path> call does not
    # leave stale handlers (idempotency for test isolation).
    # ------------------------------------------------------------------
    if data_dir is not None:
        _setup_file_channels(
            root_logger=root_logger,
            clock=clock,
            data_dir=data_dir,
            level=level,
            filters=filters,
        )
    else:
        # Remove any previously-installed file handlers from child loggers.
        _remove_file_handlers(root_logger, "app")
        for child_name, channel in ((_AUDIT_LOGGER, "audit"), (_REQUESTS_LOGGER, "requests")):
            _remove_file_handlers(logging.getLogger(child_name), channel)


def _setup_file_channels(
    *,
    root_logger: logging.Logger,
    clock: Clock,
    data_dir: Path,
    level: int,
    filters: Sequence[logging.Filter] | None,
) -> None:
    """Install the three rotating JSONL file handlers.

    Separated from ``setup_logging`` for clarity (SRP).  Called only when
    ``data_dir`` is not ``None``.

    Fail-closed strategy for audit channel: if ``logs/`` dir is unwritable,
    emit a single WARNING through ``fis_monitor`` and set a disabled-sentinel
    on the audit logger so subsequent calls are silent no-ops.
    """
    logs_dir = data_dir / "logs"

    # --- Attempt to create the logs directory ---
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        root_logger.warning(
            "setup_logging: cannot create logs dir %s (%s); "
            "file channels DISABLED (fail-closed)",
            logs_dir,
            exc,
        )
        _mark_audit_disabled()
        return

    # --- app.jsonl — mirrors fis_monitor with StackPIIFilter ---
    _remove_file_handlers(root_logger, "app")
    try:
        app_handler = _make_rotating_handler(logs_dir / "app.jsonl", clock, level=level)
    except OSError as exc:
        root_logger.warning(
            "setup_logging: cannot open app.jsonl (%s); app file channel DISABLED",
            exc,
        )
    else:
        for f in filters or []:
            app_handler.addFilter(f)
        setattr(app_handler, _FILE_SENTINEL_PREFIX + "app", True)
        root_logger.addHandler(app_handler)

    # --- audit.jsonl — separate logger, NO filter, fail-closed ---
    _setup_audit_channel(logs_dir=logs_dir, clock=clock, level=level)

    # --- requests.jsonl — separate logger, whitelist-only ---
    _setup_requests_channel(logs_dir=logs_dir, clock=clock, level=level)


# Sentinel attribute name for the "audit disabled" state.
_AUDIT_DISABLED_ATTR = "_fis_monitor_audit_disabled"


def _mark_audit_disabled() -> None:
    """Mark the audit logger as disabled (fail-closed)."""
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    setattr(audit_logger, _AUDIT_DISABLED_ATTR, True)


def _setup_audit_channel(*, logs_dir: Path, clock: Clock, level: int) -> None:
    """Wire the ``fis_monitor.audit`` logger to ``audit.jsonl``."""
    audit_logger = logging.getLogger(_AUDIT_LOGGER)

    # Clear the disabled sentinel (new setup_logging call = fresh start).
    setattr(audit_logger, _AUDIT_DISABLED_ATTR, False)

    # Remove any previously-installed audit file handler (idempotency).
    _remove_file_handlers(audit_logger, "audit")

    try:
        audit_handler = _make_rotating_handler(
            logs_dir / "audit.jsonl", clock, level=level
        )
    except OSError as exc:
        logging.getLogger(_FIS_MONITOR_LOGGER).warning(
            "setup_logging: cannot open audit.jsonl (%s); "
            "audit channel DISABLED (fail-closed)",
            exc,
        )
        _mark_audit_disabled()
        return

    setattr(audit_handler, _FILE_SENTINEL_PREFIX + "audit", True)
    audit_logger.setLevel(level)
    # propagate=False: audit records must NOT reach the parent fis_monitor
    # handler (which has StackPIIFilter) — PII is intentional in audit.
    audit_logger.propagate = False
    audit_logger.addHandler(audit_handler)


def _setup_requests_channel(*, logs_dir: Path, clock: Clock, level: int) -> None:
    """Wire the ``fis_monitor.requests`` logger to ``requests.jsonl``."""
    req_logger = logging.getLogger(_REQUESTS_LOGGER)

    _remove_file_handlers(req_logger, "requests")

    try:
        req_handler = _make_rotating_handler(
            logs_dir / "requests.jsonl", clock, level=level
        )
    except OSError as exc:
        logging.getLogger(_FIS_MONITOR_LOGGER).warning(
            "setup_logging: cannot open requests.jsonl (%s); "
            "requests channel DISABLED",
            exc,
        )
        return

    setattr(req_handler, _FILE_SENTINEL_PREFIX + "requests", True)
    req_logger.setLevel(level)
    # propagate=False: requests records handled exclusively by their own channel.
    req_logger.propagate = False
    req_logger.addHandler(req_handler)


# ---------------------------------------------------------------------------
# URL query policy (canon: 10-9-http-logs.md)
# ---------------------------------------------------------------------------

# Login routes whose query string is masked as ``?<redacted>`` rather than
# dropped entirely (canon §10.9: "Для логин-роутов query замаскирована как
# ``?<redacted>``").  Add new login-related route prefixes here to extend
# the policy without modifying any other code.
_LOGIN_ROUTE_PREFIXES: frozenset[str] = frozenset({"/auth", "/login"})


def _strip_query(url: str) -> str:
    """Return *url* with query string AND fragment removed.

    For full URLs (``http://`` / ``https://``) uses ``urlsplit`` / ``urlunsplit``
    so that ports and unusual paths are parsed correctly.  Fragments are
    dropped consistently in both branches.

    For bare paths (no scheme) a simple string split is used.

    Examples::

        >>> _strip_query("https://host/path?token=abc")
        'https://host/path'
        >>> _strip_query("https://host/path#frag")
        'https://host/path'
        >>> _strip_query("/cabinet/free-lot?page=2")
        '/cabinet/free-lot'
        >>> _strip_query("/path#frag")
        '/path'
        >>> _strip_query("/path?q=1#frag")
        '/path'
        >>> _strip_query("/search")
        '/search'
    """
    if "?" not in url and "#" not in url:
        return url
    # For URLs with a scheme, use proper parsing (handles ports, netloc, etc.).
    if url.startswith("http://") or url.startswith("https://"):
        parts = urlsplit(url)
        # Drop both query and fragment consistently.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    # For bare paths (no scheme): strip query then fragment.
    return url.partition("?")[0].partition("#")[0]


def _apply_query_policy(url_or_path: str) -> str:
    """Apply the canon §10.9 query policy and return a log-safe path.

    Rules (in priority order):

    1. No query → return bare path as-is (fragment also stripped).
    2. Path starts with a ``_LOGIN_ROUTE_PREFIXES`` prefix → return
       ``<path>?<redacted>`` (mask, not drop — canon requirement).
    3. All other paths → return path without query (``_strip_query`` behaviour).

    The return value is always a bare path (no scheme / netloc), since
    ``requests.jsonl`` stores ``url_path``, not full URLs.

    Examples::

        >>> _apply_query_policy("/login?token=abc")
        '/login?<redacted>'
        >>> _apply_query_policy("/auth/oauth/callback?code=XYZ")
        '/auth/oauth/callback?<redacted>'
        >>> _apply_query_policy("https://host/login?token=abc")
        '/login?<redacted>'
        >>> _apply_query_policy("/cabinet/free-lot?page=2")
        '/cabinet/free-lot'
        >>> _apply_query_policy("/search")
        '/search'
    """
    # Extract bare path component (drop scheme + netloc for full URLs).
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        parts = urlsplit(url_or_path)
        path = parts.path
        has_query = bool(parts.query)
    else:
        # Bare path: split off query and fragment.
        no_frag = url_or_path.partition("#")[0]
        path, _, raw_query = no_frag.partition("?")
        has_query = bool(raw_query)

    # Rule 1: no query — return clean path (fragment already stripped above).
    if not has_query:
        return path

    # Rule 2: login routes — mask query.
    for prefix in _LOGIN_ROUTE_PREFIXES:
        if path.startswith(prefix):
            return path + "?<redacted>"

    # Rule 3: all other routes — drop query entirely.
    return path


# ---------------------------------------------------------------------------
# log_request — whitelist-only HTTP access log helper
# ---------------------------------------------------------------------------


def log_request(
    method: str,
    url_path: str,
    status: int,
    duration_ms: float,
    bytes: int,
    *,
    parser_version: str | None = None,
) -> None:
    """Write one HTTP access record to ``requests.jsonl``.

    Enforces the field whitelist from canon ``docs/architecture/10-9-http-logs.md``:
    only ``method``, ``url_path`` (query masked or stripped per policy),
    ``status``, ``duration_ms``, ``bytes``, and optionally ``parser_version``
    are written.

    Never writes: ``Cookie``, ``Authorization``, ``Set-Cookie``, body, or raw query.

    No ``**kwargs`` by design — any new field must be explicitly added to this
    signature AND to the canon whitelist in ``docs/architecture/10-9-http-logs.md``.
    Adding ``**kwargs`` here would silently bypass the whitelist enforcement.

    Query policy (canon §10.9):
    - Login routes (``/auth*``, ``/login*``) → ``?<redacted>`` appended.
    - All other routes → query dropped entirely.

    Args:
        method:         HTTP method (e.g. ``"GET"``).
        url_path:       Request path, with or without query — query is
                        masked or stripped per ``_apply_query_policy``.
        status:         HTTP response status code.
        duration_ms:    Request duration in milliseconds.
        bytes:          Response body size in bytes.
        parser_version: Optional parser version tag (e.g. ``"v3"``).
    """
    req_logger = logging.getLogger(_REQUESTS_LOGGER)
    # Apply canon §10.9 query policy: mask login routes, strip all others.
    # No **kwargs — whitelist is enforced by the fixed signature above.
    clean_path = _apply_query_policy(url_path)
    req_logger.info(
        "HTTP %s %s %d",
        method,
        clean_path,
        status,
        extra={
            "method": method,
            "url_path": clean_path,
            "status": status,
            "duration_ms": duration_ms,
            "bytes": bytes,
            **({"parser_version": parser_version} if parser_version is not None else {}),
        },
    )


# ---------------------------------------------------------------------------
# Audit logger public helper — respects fail-closed sentinel
# ---------------------------------------------------------------------------


def log_audit(message: str, **kwargs: Any) -> None:
    """Write one record to ``audit.jsonl`` (PII-allowed channel).

    If the audit channel is disabled (fail-closed due to unwritable logs dir),
    this function is a silent no-op — no exception escapes.

    Args:
        message: Human-readable audit event description.
        **kwargs: Additional context fields (included in ``ctx`` by JsonFormatter).
    """
    audit_logger = logging.getLogger(_AUDIT_LOGGER)
    if getattr(audit_logger, _AUDIT_DISABLED_ATTR, False):
        return
    audit_logger.info(message, extra=kwargs)


# ---------------------------------------------------------------------------
# get_logger — thin alias
# ---------------------------------------------------------------------------


def get_logger(name: str) -> logging.Logger:
    """Return a ``logging.Logger`` for *name*.

    Thin wrapper so callers stay decoupled from the stdlib import while we
    retain the ability to swap the implementation later.

    Usage::

        from fis_monitor.utils.log import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)
