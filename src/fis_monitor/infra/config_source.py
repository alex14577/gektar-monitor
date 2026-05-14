"""WatchdogConfigSource — live config hot-reload via filesystem watchdog.

Implements the ``ConfigSource`` Protocol (domain/interfaces.py:216-233).

Design decisions (brainstorm consensus, bye.9):
  - Watches the *directory* containing config.json, not the file itself.
    Atomic saves (os.replace / mv) emit ``FileMovedEvent`` or ``FileCreatedEvent``
    on the parent directory; watching the file would miss them on many platforms.
  - ``FileMovedEvent.dest_path`` filter: inotify on Linux emits dest_path for
    atomic replace — we filter on that, not src_path (see BA-5 in brainstorm).
  - Ref-swap without RLock on read: CPython GIL makes LOAD_ATTR/STORE_ATTR
    atomic for a single object reference.  Write happens only in watchdog-thread
    under ``_lock``.  Read (``current()``) is lock-free.
    NOTE: CPython GIL guarantees atomic ref read/write.  Revisit for PEP-703.
  - Debounce 300 ms via ``threading.Timer``: coalesces editor-write bursts.
  - Content-hash dedup (SHA-256 of first 4 KB): prevents repeated parse
    attempts and log-spam for identical content.
  - MAX_CONFIG_SIZE = 1 MB: anti-OOM guard before reading full file.
  - Reload errors: keep old snapshot, log warning without PII (no raw exc for
    ValidationError, only error count).

See: docs/architecture/07-concurrency.md §7.6, ADR-020.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from fis_monitor.domain.models import Settings
from fis_monitor.infra.sse.subscriptions import ThreadConfigSubscription

if TYPE_CHECKING:
    from fis_monitor.domain.interfaces import Clock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_CONFIG_SIZE = 1 * 1024 * 1024  # 1 MB
_HASH_SAMPLE_SIZE = 4 * 1024  # first 4 KB for content-hash dedup
_DEBOUNCE_SECONDS = 0.3

# ---------------------------------------------------------------------------
# Default parser (module-level DI seam for tests)
# ---------------------------------------------------------------------------
_default_parser: Callable[[bytes], Settings] = Settings.model_validate_json


# ---------------------------------------------------------------------------
# Watchdog event handler (private, high cohesion)
# ---------------------------------------------------------------------------


class _ConfigFileEventHandler(FileSystemEventHandler):
    """Watchdog event handler that notifies the owner on relevant FS events.

    Filters events by ``target_name`` (basename of the watched config file).
    Delegates to ``on_relevant_event`` callback instead of coupling tightly
    to ``WatchdogConfigSource`` — allows unit tests to inject a counter/mock.
    """

    def __init__(
        self,
        target_name: str,
        on_relevant_event: Callable[[], None],
    ) -> None:
        super().__init__()
        self._target_name = target_name
        self._on_relevant_event = on_relevant_event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _matches(self, event: object) -> bool:
        """Return True if *event* concerns the watched config file.

        ``FileMovedEvent`` carries the final path in ``dest_path``
        (inotify IN_MOVED_TO), while other events use ``src_path``.
        We extract the relevant path and compare basename.
        """
        dest = getattr(event, "dest_path", None)
        # For FileMovedEvent, check dest_path; for all others, src_path.
        relevant_path = dest if dest else getattr(event, "src_path", "")
        return Path(relevant_path).name == self._target_name

    # ------------------------------------------------------------------
    # Watchdog overrides
    # ------------------------------------------------------------------

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and self._matches(event):
            self._on_relevant_event()

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and self._matches(event):
            self._on_relevant_event()

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and self._matches(event):
            self._on_relevant_event()


# ---------------------------------------------------------------------------
# WatchdogConfigSource
# ---------------------------------------------------------------------------


class WatchdogConfigSource:
    """Live configuration stream backed by a filesystem watchdog observer.

    Implements the ``ConfigSource`` Protocol (domain/interfaces.py:216-233).

    Constructor starts the watchdog Observer immediately.  Call ``stop()``
    during application shutdown to join the observer thread.

    Args:
        path:   Absolute path to ``config.json``.
        clock:  ``Clock`` implementation for observability timestamps.
        parser: DI seam — defaults to ``Settings.model_validate_json``.
                Tests inject a lambda to avoid touching the real Pydantic model.

    Thread-safety:
        ``_current`` is written only under ``_lock`` in the watchdog thread.
        ``current()`` reads ``_current`` without a lock — safe under CPython GIL.
        NOTE: CPython GIL guarantees atomic ref read/write.  Revisit for PEP-703.

    Observability (public properties — NOT part of ConfigSource Protocol):
        reload_count:       Successful Settings swaps.
        reload_error_count: Failed parse attempts.
        last_reload_at:     UTC datetime of last successful swap (or None).
        last_error_at:      UTC datetime of last parse failure (or None).
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock,
        parser: Callable[[bytes], Settings] = _default_parser,
    ) -> None:
        self._path = path
        self._clock = clock
        self._parser = parser

        # Bootstrap: use defaults if file absent.
        if path.exists():
            try:
                self._current: Settings = parser(path.read_bytes())
            except Exception:
                logger.warning(
                    "config_source: failed to parse %s at startup; using defaults",
                    path.name,
                )
                self._current = Settings()
        else:
            self._current = Settings()
            logger.info(
                "config_source: %s not found, using defaults; watching for creation",
                path.name,
            )

        # Content-hash of the last successfully parsed content (or b"" for defaults).
        self._last_content_hash: bytes = b""

        # Lock guards: _current (writes), _pending_timer, _subscribers,
        # reload_count, reload_error_count, last_reload_at, last_error_at.
        self._lock = threading.Lock()

        # Debounce timer (replaced on each relevant FS event).
        self._pending_timer: threading.Timer | None = None

        # Subscriber list — protected by _lock.
        self._subscribers: list[ThreadConfigSubscription] = []

        # Observability counters.
        self.reload_count: int = 0
        self.reload_error_count: int = 0
        self.last_reload_at: datetime | None = None
        self.last_error_at: datetime | None = None

        # Start watchdog observer on the *directory* (not the file).
        handler = _ConfigFileEventHandler(
            target_name=path.name,
            on_relevant_event=self._on_event,
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(path.parent), recursive=False)
        self._observer.start()

    # ------------------------------------------------------------------
    # ConfigSource Protocol implementation
    # ------------------------------------------------------------------

    def current(self) -> Settings:
        """Return the most recently loaded Settings snapshot (lock-free read).

        NOTE: CPython GIL guarantees atomic ref read/write.  Revisit for PEP-703.
        """
        return self._current

    def subscribe(self, cb: Callable[[Settings], None]) -> ThreadConfigSubscription:
        """Register a callback invoked on every successful config reload.

        Returns a ``ThreadConfigSubscription`` context-manager handle.
        Callers SHOULD use it as a context manager to guarantee ``unsubscribe()``.
        """
        sub = ThreadConfigSubscription(cb=cb, remover=self._remove_subscriber)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def save(self, settings: Settings) -> None:
        """Atomically replace the on-disk config file with ``settings``.

        Algorithm (per ADR-023):
        1. Acquire ``_lock`` to prevent interleaving with the reload-handler.
        2. Serialise ``settings`` to JSON bytes.
        3. Write to a temp-file in the same directory (``<config>.tmp.<random8>``).
        4. ``os.replace(tmp, self._path)`` — atomic POSIX rename.
        5. Update ``_current`` and content-hash optimistically so the inotify
           event emitted by the rename does not cause a redundant subscriber
           notification (hash-dedup in ``_do_reload`` will skip the reload
           because the hash is already recorded).
        6. On any error: clean up temp file; re-raise.

        Thread-safety: the entire operation runs under ``_lock``.
        """
        raw: bytes = settings.model_dump_json(indent=2).encode()
        digest = hashlib.sha256(raw[:_HASH_SAMPLE_SIZE]).digest()

        tmp_path = self._path.parent / f"{self._path.name}.tmp.{secrets.token_hex(4)}"
        try:
            with self._lock:
                tmp_path.write_bytes(raw)
                os.replace(tmp_path, self._path)
                # Optimistic update: prevents the self-triggered watchdog event
                # from double-firing subscribers (hash-dedup skips identical hash).
                self._current = settings
                self._last_content_hash = digest
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the watchdog observer and cancel any pending debounce timer.

        Idempotent — safe to call multiple times.
        """
        with self._lock:
            timer = self._pending_timer
            self._pending_timer = None

        if timer is not None:
            timer.cancel()

        self._observer.stop()
        self._observer.join(timeout=5.0)
        if self._observer.is_alive():
            logger.warning(
                "config_source: Observer failed to stop within 5 s"
            )

    # ------------------------------------------------------------------
    # Internal — FS event → debounce → reload
    # ------------------------------------------------------------------

    def _on_event(self) -> None:
        """Schedule/reset the debounce timer on a relevant FS event."""
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            timer = threading.Timer(_DEBOUNCE_SECONDS, self._do_reload)
            self._pending_timer = timer
        timer.start()

    def _do_reload(self) -> None:
        """Read, validate, and swap Settings.  Called in watchdog/timer thread."""
        # Clear pending timer reference (we're inside it now).
        with self._lock:
            self._pending_timer = None

        # --- Size guard (anti-OOM) ---
        try:
            size = self._path.stat().st_size
        except OSError as exc:
            logger.warning("config_source: stat failed: %s", exc.__class__.__name__)
            with self._lock:
                self.reload_error_count += 1
                self.last_error_at = self._clock.now()
            return

        if size > MAX_CONFIG_SIZE:
            logger.warning(
                "config_source: %s too large: %d bytes (max %d); skipping reload",
                self._path.name,
                size,
                MAX_CONFIG_SIZE,
            )
            with self._lock:
                self.reload_error_count += 1
                self.last_error_at = self._clock.now()
            return

        # --- Read ---
        try:
            raw: bytes = self._path.read_bytes()
        except OSError as exc:
            logger.warning(
                "config_source: read failed: %s", exc.__class__.__name__
            )
            with self._lock:
                self.reload_error_count += 1
                self.last_error_at = self._clock.now()
            return

        # --- Content-hash dedup ---
        digest = hashlib.sha256(raw[:_HASH_SAMPLE_SIZE]).digest()
        with self._lock:
            if digest == self._last_content_hash:
                return  # identical content, skip parse entirely
            old_current = self._current

        # --- Parse ---
        try:
            new_settings = self._parser(raw)
        except Exception as exc:
            # Log parse errors without PII: no raw exc repr, no field values.
            if isinstance(exc, ValidationError):
                logger.warning(
                    "config_source: reload failed: %d validation error(s)",
                    len(exc.errors()),
                )
            elif isinstance(exc, json.JSONDecodeError):
                logger.warning(
                    "config_source: reload: invalid JSON at line %d", exc.lineno
                )
            else:
                logger.warning(
                    "config_source: reload: unexpected error: %s",
                    exc.__class__.__name__,
                )
            with self._lock:
                self._last_content_hash = digest  # remember hash to avoid repeated warning
                self.reload_error_count += 1
                self.last_error_at = self._clock.now()
            return

        # --- Swap (idempotent if equal) ---
        with self._lock:
            self._last_content_hash = digest
            self._current = new_settings
            self.reload_count += 1
            self.last_reload_at = self._clock.now()
            subscribers_snapshot = list(self._subscribers)

        # Skip callback delivery if Settings identical (Settings is frozen/comparable).
        if new_settings == old_current:
            return

        # Deliver to each subscriber in isolation (one crash must not drop others).
        for sub in subscribers_snapshot:
            try:
                sub.deliver(new_settings)
            except Exception:
                logger.exception(
                    "config_source: subscriber callback raised; continuing delivery"
                )

    # ------------------------------------------------------------------
    # Internal — subscriber management
    # ------------------------------------------------------------------

    def _remove_subscriber(self, sub: ThreadConfigSubscription) -> None:
        """Remover callback passed to each ThreadConfigSubscription."""
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(sub)
