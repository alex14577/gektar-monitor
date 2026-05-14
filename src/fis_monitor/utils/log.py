"""Structured JSON logging utilities for fis_monitor.

Provides:
- ``JsonFormatter`` — formats log records as single-line JSON (Clock-injected timestamps).
- ``setup_logging`` — idempotent handler registration on the ``fis_monitor`` logger.
- ``get_logger`` — thin alias over ``logging.getLogger`` for consistent naming.

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

See: docs/architecture/02-layers-dip.md for layer placement rationale.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import Clock

# ---------------------------------------------------------------------------
# Sentinel attribute name placed on the fis_monitor logger to detect
# whether our handler has already been installed.
# ---------------------------------------------------------------------------
_SENTINEL = "_fis_monitor_json_handler_installed"

# Top-level keys that cannot be overridden by caller-supplied extras.
_RESERVED_KEYS = frozenset(
    {"timestamp", "level", "logger", "message", "service", "trace_id", "exc"}
)

_FIS_MONITOR_LOGGER = "fis_monitor"


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
# setup_logging
# ---------------------------------------------------------------------------


def setup_logging(
    *,
    clock: Clock,
    stream: TextIO = sys.stdout,
    level: int = logging.INFO,
    json_format: bool = True,
) -> None:
    """Install (or replace) a handler on the ``fis_monitor`` logger.

    Idempotent: if our handler is already installed, the existing handler is
    **replaced** (so a second call with different ``stream``/``level`` takes
    effect, e.g. bootstrap stderr → lifespan stdout).  This avoids duplicate
    output while still allowing reconfiguration.

    Args:
        clock:       ``Clock`` for JSON timestamp generation.
        stream:      Output stream (``sys.stdout`` in production,
                     ``sys.stderr`` for bootstrap, ``io.StringIO`` in tests).
        level:       Logging level (``logging.INFO`` default).
        json_format: ``True`` → ``JsonFormatter``; ``False`` → plain text.
    """
    root_logger = logging.getLogger(_FIS_MONITOR_LOGGER)

    # Remove any previously-installed handler of ours (idempotency).
    for handler in list(root_logger.handlers):
        if getattr(handler, _SENTINEL, False):
            root_logger.removeHandler(handler)
            handler.close()

    # Build the new handler.
    handler = logging.StreamHandler(stream)
    if json_format:
        handler.setFormatter(JsonFormatter(clock=clock))
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT))
    handler.setLevel(level)

    # Mark handler as ours so future calls can identify and replace it.
    setattr(handler, _SENTINEL, True)

    root_logger.setLevel(level)
    # propagate=True: records can still reach root (e.g. pytest caplog fixture).
    # Duplication risk is minimal: uvicorn installs its handlers on the "uvicorn"
    # and "uvicorn.access" loggers, not root, so fis_monitor records do not
    # trigger a second output path in production.
    root_logger.propagate = True
    root_logger.addHandler(handler)


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
