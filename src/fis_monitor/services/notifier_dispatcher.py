"""NotifierDispatcher — durable notification consumer with retry + recovery.

Implements the services-layer dispatch loop for the ``email`` / ``browser``
/ ``heartbeat`` channels via a durable state-machine backed by
``NotificationsRepository`` (ADR-019).

Design invariants:
- ``dispatch()`` is a fire-forget producer: puts a ``LotPublicDTO`` into an
  in-memory ``queue.Queue`` without blocking the monitor cycle.
- ``consumer_loop()`` runs in a dedicated supervised thread; it drains the
  queue and runs periodic recovery for zombie / stale-pending rows.
- Retry backoff uses ``stop_event.wait(delay)`` — NOT ``time.sleep`` — so
  the loop exits immediately on shutdown (R3-M2).
- ``mark_attempt`` returning ``None`` means the row reached a terminal
  status concurrently (R4-C4 race); the caller skips the send.
- ``MAX_TOTAL_ATTEMPTS = 10`` hard cap (R4-M6): any row that exceeds this
  is promoted to ``permanent_fail`` regardless of retryability.
- ``list_pending_older_than`` MUST include ``last_attempt_at IS NULL``
  zombie-reserves (R4-C3) — this is a repo contract; tests verify it.
- PII contract: recipient addresses are NEVER logged in plaintext; they are
  hashed to ``sha256[:8]`` for correlation only.

See docs/notifications.md §Consumer-loop and ADR-019.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import random
import threading
from collections.abc import Sequence
from datetime import timedelta
from typing import ClassVar

from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    EventBus,
    LotRepository,
    NotificationsRepository,
    RegionSubscriptionRepository,
    SettingsRepository,
)
from fis_monitor.domain.models import (
    ErrorCategory,
    LotPublicDTO,
    NotificationRecord,
    NotifyResult,
    SseSmtpFailed,
)
from fis_monitor.domain.models import (
    lot_to_public_dto as _lot_to_public_dto,
)
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from fis_monitor.services.dnd import DndService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard cap on total delivery attempts (R4-M6)
# ---------------------------------------------------------------------------
MAX_TOTAL_ATTEMPTS: int = 10

# ---------------------------------------------------------------------------
# Channel-id constants — extensible via _recipients_of without modification
# ---------------------------------------------------------------------------
_CHANNEL_EMAIL = "email"
_CHANNEL_BROWSER = "browser"
_CHANNEL_HEARTBEAT = "heartbeat"


class SubscribedAtFilteredNotifier:
    """Decorator applying subscribed_at suppression for the wrapped email Notifier.

    Suppresses send() if lot.date_create < region's subscribed_at.
    Browser/heartbeat notifiers are registered without this wrapper — they
    always receive all lots so the UI feed updates in real-time.
    """

    channel_id: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Email (subscribed_at filtered)"
    description: ClassVar[str] = "Email notifier with per-region subscribed_at suppression"
    config_schema: ClassVar[type] = type(None)  # delegated at runtime via inner notifier
    recipient_label: ClassVar[str] = "Email address"
    recipient_placeholder: ClassVar[str] = "user@example.com"

    def __init__(self, inner: object, region_sub_repo: RegionSubscriptionRepository) -> None:
        self._inner = inner
        self._region_sub_repo = region_sub_repo
        # Reflect config_schema from inner so UI forms work correctly
        inner_schema = (
            getattr(type(inner), "config_schema", None)
            or getattr(inner, "config_schema", None)
        )
        if inner_schema is not None:
            type(self).config_schema = inner_schema

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        if lot.region_id is not None:
            subscribed_at = self._region_sub_repo.get_subscribed_at(lot.region_id)
            if subscribed_at is not None and lot.date_create < subscribed_at:
                logger.debug(
                    "notification.subscribed_at_dropped",
                    extra={
                        "region_id": lot.region_id,
                        "lot_id": lot.id,
                        "lot_date_create": lot.date_create.isoformat(),
                        "subscribed_at": subscribed_at.isoformat(),
                        "decision": "dropped_subscribed_at",
                    },
                )
                return NotifyResult(ok=True, detail="suppressed (subscribed_at)", retryable=False)
        return self._inner.send(lot, recipient)  # type: ignore[attr-defined]

    def test(self, recipient: str) -> NotifyResult:
        return self._inner.test(recipient)  # type: ignore[attr-defined]


def _classify_error(result: NotifyResult) -> ErrorCategory:
    """Map a ``NotifyResult`` failure to a closed ``ErrorCategory``.

    Uses ``retryable`` as the primary signal — retryable failures are
    transient network/service errors; non-retryable are treated as
    ``http_4xx`` (auth / bad-request class) unless the detail suggests
    an internal fault.

    This keeps ``error_category`` semantically useful while preserving
    the invariant that ``SseSmtpFailed.error_category`` is always a
    valid ``ErrorCategory`` member (closed enum, never a raw exception
    class name).
    """
    if result.retryable:
        return "network"
    detail_lower = (result.detail or "").lower()
    if "timeout" in detail_lower:
        return "timeout"
    if "5xx" in detail_lower or "server error" in detail_lower:
        return "http_5xx"
    return "http_4xx"


class NotifierDispatcher:
    """Durable notification dispatcher with consumer loop, retry, and recovery.

    All state related to delivery is persisted in ``NotificationsRepository``
    so that restarts do not lose pending notifications (at-least-once semantic,
    ADR-019 R4-C5).

    Args:
        registry:       Explicit notifier registry (infra/notifiers/registry.py).
        notif_repo:     Notifications state-machine repository.
        lot_repo:       Lot repository — used by recovery to reload lot data by id.
        config_source:  Live config — read once per ``_recipients_of`` call.
        clock:          Injected time source (testable).
        event_bus:      SSE fan-out bus — used to publish ``SseSmtpFailed``.
        stop_event:     Shared shutdown signal. ``consumer_loop`` exits when set.
        settings_repo:  Optional settings repository (currently unused, reserved).
        retry_attempts: Max send attempts per ``_send_one`` invocation.
        retry_backoff:  Per-attempt sleep durations (seconds). Index clamped to
                        ``len - 1`` so it works for any ``retry_attempts`` count.
        max_queue_size: In-memory queue capacity. On overflow, lots are dropped
                        with a warning (producer monitor-cycle takes priority).
        recovery_age:   Minimum age of a pending row before recovery picks it up.
    """

    def __init__(
        self,
        *,
        registry: ExplicitNotifierRegistry,
        notif_repo: NotificationsRepository,
        lot_repo: LotRepository,
        config_source: ConfigSource,
        clock: Clock,
        event_bus: EventBus,
        stop_event: threading.Event,
        dnd_service: DndService,
        settings_repo: SettingsRepository | None = None,
        retry_attempts: int = 3,
        retry_backoff: Sequence[float] = (2.0, 4.0, 8.0),
        max_queue_size: int = 10_000,
        recovery_age: timedelta = timedelta(minutes=1),
    ) -> None:
        self._registry = registry
        self._notif_repo = notif_repo
        self._lot_repo = lot_repo
        self._config_source = config_source
        self._clock = clock
        self._event_bus = event_bus
        self.stop_event = stop_event
        self._dnd_service = dnd_service
        self._settings_repo = settings_repo
        self.retry_attempts = retry_attempts
        self.retry_backoff = list(retry_backoff)
        self.recovery_age = recovery_age

        self._queue: queue.Queue[LotPublicDTO] = queue.Queue(maxsize=max_queue_size)

    # ------------------------------------------------------------------
    # Public producer interface
    # ------------------------------------------------------------------

    def dispatch(self, lot: LotPublicDTO) -> None:
        """Fire-and-forget: enqueue ``lot`` for async delivery.

        On queue overflow the lot is silently dropped and a warning is
        logged — monitor-cycle throughput takes priority over notifications.
        subscribed_at filtering is applied per-channel by SubscribedAtFilteredNotifier.
        """
        logger.debug(
            "dispatcher.dispatch.entry",
            extra={
                "lot_id": lot.id,
                "region_id": lot.region_id,
                "channels_count": len(list(self._registry.all())),
            },
        )
        try:
            self._queue.put_nowait(lot)
        except queue.Full:
            logger.warning(
                "dispatcher.queue_full",
                extra={"lot_id": lot.id},
            )

    # ------------------------------------------------------------------
    # Consumer loop (runs in a supervised thread)
    # ------------------------------------------------------------------

    def consumer_loop(self) -> None:
        """Synchronous consumer loop — NOT asyncio (R4-M11).

        Drains the in-memory queue; then runs recovery for stale-pending
        rows (including zombie ``last_attempt_at IS NULL`` rows, R4-C3).

        Exits when ``self.stop_event`` is set.
        """
        while not self.stop_event.is_set():
            # 1) Drain one lot from the queue
            try:
                lot = self._queue.get(timeout=1.0)
            except queue.Empty:
                lot = None

            if lot is not None:
                self._dispatch_all_channels(lot)

            # 2) Recovery: pick up stale / zombie pending rows
            for pending in self._notif_repo.list_pending_older_than(self.recovery_age):
                if self.stop_event.is_set():
                    return
                self._retry_one(pending)

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_all_channels(self, lot: LotPublicDTO) -> None:
        """Deliver ``lot`` through every registered notifier x recipient pair.

        Do-Not-Disturb guard: if DnD is active at dispatch time, all channel
        deliveries are suppressed for the duration of the DnD window.  The
        lot is not re-queued — the monitor cycle produces a new event on the
        next scan if the lot is still relevant.  See dnd.py docstring.
        """
        if self._dnd_service.is_active(self._clock.now()):
            logger.info("dispatch suppressed (DnD active)")
            return
        for notifier in self._registry.all():
            channel_id_log: str = type(notifier).channel_id  # type: ignore[attr-defined]
            recipients = self._recipients_of(notifier)
            for recipient in recipients:
                self._send_one(lot, notifier, recipient)
            logger.debug(
                "dispatcher.channel.invoked",
                extra={
                    "lot_id": lot.id,
                    "channel_id": channel_id_log,
                    "recipients_count": len(recipients),
                },
            )

    def _recipients_of(self, notifier: object) -> list[str]:
        """Derive the recipient list for a given notifier instance.

        Email channel: reads ``config.notifications.email.recipients``.
        Browser / heartbeat channels: singleton ``"local"`` pseudo-address.
        Unknown channel_id: returns ``[]`` (extensibility — no raise).
        """
        # Access channel_id via the class to match Protocol ClassVar convention
        channel_id: str = type(notifier).channel_id  # type: ignore[attr-defined]
        if channel_id == _CHANNEL_EMAIL:
            return list(self._config_source.current().notifications.email.recipients)
        if channel_id in (_CHANNEL_BROWSER, _CHANNEL_HEARTBEAT):
            return ["local"]
        # Unknown channel — return empty (OCP: new channels don't break existing code)
        logger.debug(
            "dispatcher.unknown_channel_recipients",
            extra={"channel_id": channel_id},
        )
        return []

    def _send_one(
        self,
        lot: LotPublicDTO,
        notifier: object,
        recipient: str,
    ) -> None:
        """Attempt delivery for one (lot, channel, recipient) triplet.

        Flow:
        1. Reserve the slot (idempotent).
        2. Retry loop up to ``retry_attempts`` times.
        3. On success → ``mark_sent``.
        4. On non-retryable failure → ``mark_permanent_fail`` + publish event.
        5. On shutdown during backoff → leave ``pending`` (recovery picks up).
        6. On exhaustion of retry loop → leave ``pending`` (recovery picks up).
        7. On ``attempt_no > MAX_TOTAL_ATTEMPTS`` → ``mark_permanent_fail`` (cap).
        """
        channel_id: str = type(notifier).channel_id  # type: ignore[attr-defined]
        notifier_send = notifier.send  # type: ignore[attr-defined]

        # --- Step 1: reserve (idempotent INSERT OR IGNORE) --------------------
        status = self._notif_repo.status_of(lot.id, channel_id, recipient)
        if status in ("sent", "permanent_fail"):
            return
        if status is None:
            self._notif_repo.reserve(lot.id, channel_id, recipient)

        result = None  # track last result for exhausted-loop publish

        # --- Step 2: retry loop ------------------------------------------------
        for _ in range(self.retry_attempts):
            attempt_no = self._notif_repo.mark_attempt(
                lot.id, channel_id, recipient, at=self._clock.now()
            )
            # R4-C4: concurrent consumer or recovery moved row to terminal status
            if attempt_no is None:
                return

            # R4-M6: hard cap — too many cumulative attempts across restarts
            if attempt_no > MAX_TOTAL_ATTEMPTS:
                self._notif_repo.mark_permanent_fail(lot.id, channel_id, recipient)
                recipient_hash = hashlib.sha256(
                    recipient.encode("utf-8")
                ).hexdigest()[:8]
                logger.warning(
                    "notification.cap_reached",
                    extra={
                        "lot_id": lot.id,
                        "channel": channel_id,
                        "attempt_no": attempt_no,
                        "recipient_hash": recipient_hash,
                    },
                )
                return

            result = notifier_send(lot, recipient)

            if result.ok:
                self._notif_repo.mark_sent(
                    lot.id, channel_id, recipient, at=self._clock.now()
                )
                return

            if not result.retryable:
                self._notif_repo.mark_permanent_fail(lot.id, channel_id, recipient)
                self._publish_smtp_failed(lot, channel_id, attempt_no, result)
                return

            # Retryable failure — stop_event-aware backoff sleep (R3-M2)
            idx = min(attempt_no - 1, len(self.retry_backoff) - 1)
            delay = self.retry_backoff[idx] + random.uniform(0, 0.5)
            if self.stop_event.wait(delay):
                # Shutdown received during sleep — leave status='pending'
                # so recovery on next start can pick up.
                return

        # All retry_attempts exhausted — do NOT mark permanent_fail.
        # Leave status='pending' so recovery / next consumer cycle retries.
        if result is not None:
            self._publish_smtp_failed(lot, channel_id, self.retry_attempts, result)

    def _retry_one(self, pending: NotificationRecord) -> None:
        """Recovery path: re-attempt delivery for a stale-pending record.

        Loads the full lot from ``lot_repo``. If the lot no longer exists
        (e.g. hard-removed), promotes the notification to ``permanent_fail``.

        If the channel is not registered in the registry (e.g. plugin removed),
        logs a warning and skips — does NOT raise.
        """
        # Resolve notifier
        if not self._registry.has(pending.channel):
            logger.warning(
                "dispatcher.retry_unknown_channel",
                extra={"channel": pending.channel, "lot_id": pending.lot_id},
            )
            return

        notifier = self._registry.get(pending.channel)

        # Load full lot for send context
        lot = self._lot_repo.get(pending.lot_id)
        if lot is None:
            logger.warning(
                "dispatcher.retry_lot_missing",
                extra={"lot_id": pending.lot_id, "channel": pending.channel},
            )
            self._notif_repo.mark_permanent_fail(
                pending.lot_id, pending.channel, pending.recipient
            )
            return

        # Convert Lot → LotPublicDTO before passing to _send_one.
        # _send_one calls notifier.send(lot, recipient); BrowserSseNotifier builds
        # SseLotNew(lot=lot) which requires a LotPublicDTO — passing a bare Lot
        # causes a Pydantic ValidationError that is silently caught as a
        # non-retryable failure, resulting in a false mark_sent (P0-3 bug fix).
        lot_dto = _lot_to_public_dto(lot)
        self._send_one(
            lot_dto,
            notifier,
            pending.recipient,
        )

    def _publish_smtp_failed(
        self,
        lot: LotPublicDTO,
        channel_id: str,
        attempt_no: int,
        result: NotifyResult,
    ) -> None:
        """Publish ``SseSmtpFailed`` on the event bus (PII-safe).

        ``result.detail`` is truncated to 200 chars before logging;
        it is NOT included in the SSE payload (see ``SseSmtpFailed`` model).
        The recipient address is NEVER included.

        ``error_category`` is derived via ``_classify_error()`` so it is
        always a valid closed-``ErrorCategory`` member — never a raw
        exception class name or free-form string.
        """
        detail: str = result.detail or ""
        detail_safe = detail[:200] if detail else ""
        logger.warning(
            "dispatcher.smtp_failed",
            extra={
                "lot_id": lot.id,
                "channel_id": channel_id,
                "attempt_no": attempt_no,
                "detail": detail_safe,
            },
        )
        event = SseSmtpFailed(
            timestamp=self._clock.now(),
            channel_id=channel_id,
            attempt_no=attempt_no,
            error_category=_classify_error(result),
        )
        self._event_bus.publish(event)
