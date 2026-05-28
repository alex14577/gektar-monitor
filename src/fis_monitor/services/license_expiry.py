"""LicenseExpirySupervisor — daily runtime license check.

Architecture: services layer (Layer 2).

Checks the license key once immediately on start and then every day at
00:01 UTC (``next_check_at`` pure function).  On expiry or any invalid
state triggers graceful shutdown via ``ShutdownRequester`` and arms a
watchdog timer that hard-exits (``os._exit(1)``) if graceful shutdown does
not complete within ``watchdog_grace_seconds`` (default 45 s).

Design invariants
-----------------
- **Fail-closed on crash**: an unexpected exception in the check loop logs
  ``supervisor.crash`` and calls ``_handle_expiry(None, reason='crash')``.
  The app does NOT continue running.
- **Idempotency**: ``_expiry_lock`` + ``_expiry_handled`` bool prevent double-fire
  of the watchdog / shutdown request / SSE event even if two threads call
  ``_handle_expiry`` concurrently (Lock eliminates the check-then-act TOCTOU
  that threading.Event.is_set()+set() would have).
- **Re-read from disk**: ``LicenseKeyProvider.load_key()`` is called on every
  check — key rotation takes effect without restart (see ADR-056 §Runtime
  expiry enforcement).
- **DI seams**: ``SecretProvider``, ``LicenseKeyProvider``, ``Clock``,
  ``EventBus``, ``ShutdownRequester``, ``LicenseVerifier`` (module-local
  Protocol) are all injected through the constructor.  Production code
  passes thin adapters; tests pass fakes.

Observability (logger ``fis_monitor.services.license_expiry``)
---------------------------------------------------------------
| Event                  | Level    | Extra fields                         |
|------------------------|----------|--------------------------------------|
| supervisor.start       | INFO     | —                                    |
| check.valid            | DEBUG    | today, exp, days_until_exp           |
| check.expired          | WARNING  | today, exp                           |
| check.error            | ERROR    | error_type, today                    |
| shutdown_requested     | WARNING  | today, exp                           |
| watchdog.armed         | INFO     | grace_seconds                        |
| watchdog.fired         | CRITICAL | —                                    |
| supervisor.crash       | ERROR    | exc_type                             |
| supervisor.stop        | INFO     | —                                    |

PII policy
----------
NEVER log: key_str, secret bytes, raw payload bytes, licensee field.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from fis_monitor.domain.interfaces import (
    Clock,
    EventBus,
    LicenseKeyProvider,
    SecretProvider,
    ShutdownRequester,
)
from fis_monitor.domain.models import SseLicenseExpired
from fis_monitor.licensing._verify import LicenseResult, LicenseStatus

__all__ = ["LicenseExpirySupervisor", "next_check_at"]

logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# LicenseVerifier — module-local Protocol (services layer can import licensing)
# ---------------------------------------------------------------------------


class LicenseVerifier(Protocol):
    """Verify a license key against a secret and a wall-clock instant.

    Module-local Protocol (not in domain/interfaces) because ``LicenseResult``
    lives in the ``licensing`` package — domain cannot import from there.

    Exists solely as a DI seam for tests; production wires a thin adapter.
    """

    def verify(self, key_str: str, secret: bytes, now: datetime) -> LicenseResult:
        """Return a ``LicenseResult``. Never raises.

        Args:
            key_str: License key string.
            secret:  32-byte HMAC secret.
            now:     Current UTC-aware datetime (injected from Clock).
        """
        ...


# ---------------------------------------------------------------------------
# _TimerLike — structural Protocol for watchdog timer duck-typing
# ---------------------------------------------------------------------------


class _TimerLike(Protocol):
    """Structural seam matching ``threading.Timer`` start/cancel API."""

    def start(self) -> None: ...
    def cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# next_check_at — pure scheduling function
# ---------------------------------------------------------------------------


def next_check_at(now: datetime, tz: ZoneInfo = _UTC) -> datetime:
    """Return the next 00:01 in ``tz``, strictly after ``now``, as UTC-aware.

    Args:
        now: Current UTC-aware datetime.
        tz:  Timezone in which to compute 00:01.  Defaults to UTC.

    Returns:
        UTC-aware datetime of the next 00:01:00 in ``tz``.

    Invariant: result is always strictly after ``now`` — if ``now`` is exactly
    00:01:00 local, tomorrow's 00:01:00 is returned.
    """
    now_local = now.astimezone(tz)
    candidate = now_local.replace(hour=0, minute=1, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


# ---------------------------------------------------------------------------
# _DefaultVerifier — production adapter wrapping verify_license
# ---------------------------------------------------------------------------


class _DefaultVerifier:
    """Thin adapter: wraps ``fis_monitor.licensing.verify_license``."""

    def verify(self, key_str: str, secret: bytes, now: datetime) -> LicenseResult:
        from fis_monitor.licensing import verify_license

        return verify_license(key_str, secret, now)


# ---------------------------------------------------------------------------
# LicenseExpirySupervisor
# ---------------------------------------------------------------------------


class LicenseExpirySupervisor:
    """Daily runtime license-expiry supervisor.

    Runs in a dedicated background thread (started by the lifespan
    supervisor via ``run_forever(stop_event)``).

    Args:
        secret_provider:        Supplies the 32-byte HMAC secret.
        key_provider:           Reads the license key from disk on each check.
        clock:                  Injected UTC clock.
        event_bus:              SSE bus — ``SseLicenseExpired`` is published on expiry.
        shutdown_requester:     Called (once) to request graceful shutdown.
        watchdog_grace_seconds: Hard-exit timeout after graceful-shutdown request.
        watchdog_factory:       Factory for the watchdog timer; defaults to
                                ``threading.Timer``.  Override in tests with a
                                ``FakeTimer`` factory.
        verifier:               Optional ``LicenseVerifier``; defaults to the
                                thin adapter over ``verify_license``.
    """

    def __init__(
        self,
        *,
        secret_provider: SecretProvider,
        key_provider: LicenseKeyProvider,
        clock: Clock,
        event_bus: EventBus,
        shutdown_requester: ShutdownRequester,
        watchdog_grace_seconds: float = 45.0,
        watchdog_factory: Callable[[float, Callable[[], None]], _TimerLike] | None = None,
        verifier: LicenseVerifier | None = None,
    ) -> None:
        self._secret_provider = secret_provider
        self._key_provider = key_provider
        self._clock = clock
        self._event_bus = event_bus
        self._shutdown_requester = shutdown_requester
        self._watchdog_grace_seconds = watchdog_grace_seconds
        self._watchdog_factory: Callable[[float, Callable[[], None]], _TimerLike] = (
            watchdog_factory if watchdog_factory is not None else threading.Timer  # type: ignore[assignment]
        )
        self._verifier: LicenseVerifier = verifier if verifier is not None else _DefaultVerifier()

        # Idempotency guard — Lock + bool flag; prevents double-fire of watchdog/SSE/shutdown.
        # threading.Event.is_set() + .set() is check-then-act TOCTOU under concurrent callers.
        # A Lock with a plain bool flag is atomic under the lock.
        self._expiry_lock = threading.Lock()
        self._expiry_handled = False
        # Stored so cancel_watchdog() can cancel it on successful graceful shutdown.
        self._watchdog_timer: _TimerLike | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel_watchdog(self) -> None:
        """Cancel the watchdog timer if one is armed.

        No-op if the watchdog was never armed (e.g., shutdown happened cleanly
        before expiry was detected).  Call from the lifespan finally-block
        *before* logging.shutdown() to ensure the cancel races no live log
        handlers.

        Invariant: only the lifespan finally-block should call this.
        """
        timer = self._watchdog_timer
        if timer is not None:
            timer.cancel()

    def run_forever(self, stop_event: threading.Event) -> None:
        """Blocking loop — run until ``stop_event`` is set or expiry detected.

        Should be called from a supervised background thread via
        ``ThreadSupervisor.start("license-expiry", supervisor.run_forever)``.
        """
        logger.info("license_expiry.supervisor.start")
        try:
            if self._check_once():
                return
            while not stop_event.is_set():
                target = next_check_at(self._clock.now(), _UTC)
                delay = (target - self._clock.now()).total_seconds()
                # wait() returns True when stop_event is set → clean exit.
                if stop_event.wait(max(0.0, delay)):
                    break
                # Clock-skew guard: if we woke up more than 5 s early, loop back.
                if self._clock.now() < target - timedelta(seconds=5):
                    continue
                expired = self._check_once()
                if expired:
                    break
        except Exception as exc:
            logger.error(
                "license_expiry.supervisor.crash",
                extra={"exc_type": type(exc).__name__},
                exc_info=True,
            )
            self._handle_expiry(None, reason="crash")
        finally:
            logger.info("license_expiry.supervisor.stop")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_once(self) -> bool:
        """Perform one license verification.

        Returns:
            ``True`` if the license is expired/invalid (shutdown triggered);
            ``False`` if VALID and the supervisor should keep running.
        """
        # Capture wall-clock once — prevents midnight-boundary split between
        # today derivation and the timestamp passed to the verifier.
        now_utc = self._clock.now()
        today = now_utc.date()
        try:
            key_str = self._key_provider.load_key()
        except (FileNotFoundError, OSError) as exc:
            logger.error(
                "license_expiry.check.error",
                extra={"error_type": type(exc).__name__, "today": str(today)},
            )
            self._handle_expiry(None, reason="file_missing")
            return True

        secret = self._secret_provider.get_secret()
        result = self._verifier.verify(key_str, secret, now=now_utc)

        if result.status == LicenseStatus.VALID:
            exp = result.expires_at
            days_until_exp: int | None = (exp - today).days if exp is not None else None
            logger.debug(
                "license_expiry.check.valid",
                extra={
                    "today": str(today),
                    "exp": str(exp) if exp is not None else None,
                    "days_until_exp": days_until_exp,
                },
            )
            return False

        if result.status == LicenseStatus.EXPIRED:
            logger.warning(
                "license_expiry.check.expired",
                extra={
                    "today": str(today),
                    "exp": str(result.expires_at) if result.expires_at is not None else None,
                },
            )
            self._handle_expiry(result, reason="expired")
            return True

        # INVALID or any future status → fail-closed
        logger.error(
            "license_expiry.check.error",
            extra={"error_type": result.status.name, "today": str(today)},
        )
        self._handle_expiry(result, reason=result.status.name)
        return True

    def _handle_expiry(self, result: LicenseResult | None, *, reason: str) -> None:
        """Handle license expiry — idempotent.

        Actions (executed at most once due to ``_expiry_handled``):
        1. Print a human-readable banner to stderr.
        2. Publish ``SseLicenseExpired`` on the event bus.
        3. Arm the watchdog timer.
        4. Log ``shutdown_requested``.
        5. Call ``shutdown_requester.request_shutdown()``.
        """
        # Atomic check-and-set under lock prevents TOCTOU race where two
        # concurrent callers both pass is_set() before either calls set().
        with self._expiry_lock:
            if self._expiry_handled:
                return
            self._expiry_handled = True
        # All side-effects below execute outside the lock — they are slow
        # (network I/O for SSE, timer start) and must not block other threads.

        today = self._clock.now().date()
        exp_date: date | None = result.expires_at if result is not None else None

        # Stderr banner (human-readable, always visible regardless of log config).
        if exp_date is not None:
            print(
                f"Срок действия лицензии истёк (exp={exp_date.isoformat()}). "
                "Приложение будет остановлено.",
                file=sys.stderr,
            )
        else:
            print(
                "Лицензия недействительна или отсутствует. "
                "Приложение будет остановлено.",
                file=sys.stderr,
            )

        # Publish SSE event for UI fan-out.
        try:
            self._event_bus.publish(
                SseLicenseExpired(
                    timestamp=self._clock.now(),
                    expires_at=exp_date,
                )
            )
        except Exception:
            # ERROR (not WARNING): SSE-broadcast is a security event; its failure
            # must not be silenced.
            logger.error("license_expiry._handle_expiry: event_bus.publish failed", exc_info=True)

        # Arm the watchdog: hard-exit if graceful shutdown takes too long.
        def _watchdog_fire() -> None:
            # Forensic banner written to stderr BEFORE logger.critical so it is
            # visible even if logging handlers have been shut down (M4 race).
            print(
                "WATCHDOG: license_expiry hard-exit (grace exceeded)",
                file=sys.stderr,
            )
            logger.critical("license_expiry.watchdog.fired")
            # ADR-014: grace 35s + 10s buffer = 45s. Single legitimate os._exit site.
            os._exit(1)

        timer = self._watchdog_factory(self._watchdog_grace_seconds, _watchdog_fire)
        self._watchdog_timer = timer
        timer.start()
        logger.info(
            "license_expiry.watchdog.armed",
            extra={"grace_seconds": self._watchdog_grace_seconds},
        )

        logger.warning(
            "license_expiry.shutdown_requested",
            extra={
                "today": str(today),
                "exp": str(exp_date) if exp_date is not None else None,
            },
        )
        self._shutdown_requester.request_shutdown()
