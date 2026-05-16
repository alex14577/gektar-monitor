"""SessionExpiredEmailService — one-shot email on session expiry.

Architecture: services layer (Layer 2).

Subscribes to ``SseSessionExpired`` events from the ``EventBus`` and sends
a single email notification per expiry epoch (idempotent via ``StateRepository``
key ``"session_expired_email_sent"``).

Design invariants:
- **One email per expiry epoch.**  The ``state`` key ``session_expired_email_sent``
  acts as a guard: set on first send, reset on successful login/refresh.
  Subsequent ``SseSessionExpired`` publications in the same epoch are silently
  ignored.
- **Respects email.enabled.**  If ``Settings.notifications.email.enabled`` is
  ``False`` the email is suppressed but the UI modal is NOT affected (they are
  independent paths).
- **Respects DnD.**  If the DnD window is active at the moment of the event,
  the email is suppressed and the guard is NOT set (so the next event after
  DnD expires can still send).
- **No retry / recovery loop.**  Session-expired email is fire-and-forget; the
  at-least-once guarantee from ``NotifierDispatcher`` does not apply here.
  If SMTP fails the email is lost — this is acceptable (the UI modal remains
  visible; the user will see it on next visit).
- **Recipients** are read from ``Settings.notifications.email.recipients`` at
  send time (hot-reload friendly).
- **PII policy (ADR-012):** recipient addresses are NEVER logged in plaintext;
  SHA-256[:8] hex prefix is used for log correlation.

Key: ``SESSION_EXPIRED_EMAIL_SENT_KEY = "session_expired_email_sent"``

Reset policy: the key is deleted by ``on_login_or_refresh_success()`` — a
callback registered in the composition root for both headed login AND silent
refresh outcomes with ``success=True``.

See:
    - docs/decisions/ADR-019-notification-state-machine.md (idempotency model)
    - docs/decisions/ADR-016-repository-invariants-begin-immediate.md (StateRepository)
    - docs/decisions/ADR-012-pii-isolation.md (PII contract)
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import TYPE_CHECKING

from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    EventBus,
    StateRepository,
)
from fis_monitor.domain.models import SseSessionExpired
from fis_monitor.services.dnd import DndService

if TYPE_CHECKING:
    from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier

__all__ = [
    "SESSION_EXPIRED_EMAIL_SENT_KEY",
    "SessionExpiredEmailService",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_EXPIRED_EMAIL_SENT_KEY: str = "session_expired_email_sent"
"""StateRepository key used as the idempotency guard for session-expired email."""

# How long ``consumer_loop`` blocks waiting for the next event (seconds).
# Kept short so shutdown responds quickly.
_WAIT_TIMEOUT_SECONDS: float = 1.0


class SessionExpiredEmailService:
    """Subscribe to ``SseSessionExpired`` events and send one email per expiry epoch.

    Args:
        email_notifier: SMTP notifier — ``send_session_expired(recipient)`` is
            called for each configured recipient.
        state_repo:     KV repository for idempotency guard.
        config_source:  Live config — read at send time for ``email.enabled``
            and ``email.recipients``.
        event_bus:      SSE bus to subscribe to.
        clock:          Injected UTC clock.
        dnd_service:    Do-Not-Disturb gate (checked at send time).
        stop_event:     Shared shutdown signal; ``consumer_loop`` exits when set.
    """

    def __init__(
        self,
        *,
        email_notifier: SmtpEmailNotifier,
        state_repo: StateRepository,
        config_source: ConfigSource,
        event_bus: EventBus,
        clock: Clock,
        dnd_service: DndService,
        stop_event: threading.Event,
    ) -> None:
        self._email_notifier = email_notifier
        self._state_repo = state_repo
        self._config_source = config_source
        self._event_bus = event_bus
        self._clock = clock
        self._dnd_service = dnd_service
        self.stop_event = stop_event  # public, mirrors NotifierDispatcher.stop_event

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def consumer_loop(self) -> None:
        """Blocking loop: subscribe to the event bus and process events.

        Exits when ``self._stop_event`` is set.  Should be called from a
        supervised background thread.
        """
        with self._event_bus.subscribe() as sub:
            while not self.stop_event.is_set():
                event = sub.wait_one(timeout=_WAIT_TIMEOUT_SECONDS)
                if event is None:
                    continue
                if isinstance(event, SseSessionExpired):
                    self._handle(event)

    def on_login_or_refresh_success(self) -> None:
        """Reset the idempotency guard on successful login or refresh.

        Called from the composition root after a headed login OR a silent
        refresh completes with ``success=True``.  Thread-safe: ``StateRepository``
        uses ``BEGIN IMMEDIATE`` internally (ADR-016).
        """
        self._state_repo.delete(SESSION_EXPIRED_EMAIL_SENT_KEY)
        logger.info(
            "session_expired_email: idempotency flag cleared (login/refresh success)"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle(self, event: SseSessionExpired) -> None:
        """Process one ``SseSessionExpired`` event.

        Guards (in order):
        1. Email channel must be enabled in config.
        2. Idempotency: ``session_expired_email_sent`` must be absent.
        3. DnD must not be active.

        On pass: send to every configured recipient, then set the guard.
        On DnD active: suppress silently WITHOUT setting the guard so the
        next event after DnD expires can trigger a send.
        """
        logger.info("session_expired.detected", extra={"event_type": type(event).__name__})

        cfg = self._config_source.current()
        email_cfg = cfg.notifications.email

        if not email_cfg.enabled:
            logger.debug("session_expired_email: email channel disabled — suppressed")
            return

        if self._state_repo.get(SESSION_EXPIRED_EMAIL_SENT_KEY) is not None:
            logger.debug(
                "session_expired.idempotency_skip",
                extra={"guard_key": SESSION_EXPIRED_EMAIL_SENT_KEY},
            )
            return

        now = self._clock.now()
        if self._dnd_service.is_active(now):
            logger.info("session_expired_email: DnD active — suppressed (guard NOT set)")
            return

        recipients = list(email_cfg.recipients)
        if not recipients:
            logger.info(
                "session_expired_email: no recipients configured — skipping"
            )
            # Still set the guard so we don't log this on every cycle.
            self._state_repo.set(SESSION_EXPIRED_EMAIL_SENT_KEY, "1")
            return

        any_sent = False
        for recipient in recipients:
            rh = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:8]
            result = self._email_notifier.send_session_expired(recipient)
            if result.ok:
                logger.info(
                    "session_expired_email: sent to recipient_hash=%s", rh
                )
                any_sent = True
            else:
                logger.warning(
                    "session_expired_email: send failed recipient_hash=%s detail=%s",
                    rh,
                    result.detail,
                )

        # Set guard regardless of individual delivery success — we attempted the
        # send for this epoch; retrying every cycle would spam the user.
        # The operator will see the warning in logs and can re-trigger manually.
        if any_sent or recipients:
            self._state_repo.set(SESSION_EXPIRED_EMAIL_SENT_KEY, "1")
            logger.debug(
                "session_expired.notification.queued",
                extra={"recipients_count": len(recipients)},
            )
