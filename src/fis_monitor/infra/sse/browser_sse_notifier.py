"""Browser SSE notifier — BrowserSseNotifier + BrowserNotifierConfig.

Implements the ``Notifier`` Protocol (domain/interfaces.py §Layer 3) for the
``browser`` channel. Instead of sending external notifications, this notifier
publishes a ``SseLotNew`` event to the EventBus, which fans out to connected
browser clients via Server-Sent Events (ADR-008).

**Design:**
* **Push-only** — ``test()`` is a no-op; the channel has no external endpoint.
* **Event-driven** — ``send()`` publishes to the bus; callers must handle
  EventBus overflow via the returned ``NotifyResult``.
* **DI via constructor** — EventBus dependency injected (Protocol, testable).
* **Result-pattern** — never raises for expected failures (bus overflow);
  returns ``NotifyResult``.
* **Thread-safe** — the NotifierDispatcher runs send() in a dedicated thread;
  EventBus is thread-safe (uses queue.Queue internally).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from fis_monitor.domain.interfaces import EventBus
from fis_monitor.domain.models import (
    LotPublicDTO,
    NotifierConfig,
    NotifyResult,
    SseLotNew,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BrowserNotifierConfig — plugin config schema
# ---------------------------------------------------------------------------


class BrowserNotifierConfig(NotifierConfig):
    """Per-channel configuration for :class:`BrowserSseNotifier`.

    Persisted in ``config.json`` under ``notifiers.browser``.

    Fields:
        enabled: Whether the browser channel is active.

    Note: Browser SSE is push-only; no external credentials or endpoints
    are needed. The ``recipient`` parameter in ``send()`` is ignored
    (it is always broadcast to all connected tabs).
    """

    enabled: bool = True


# ---------------------------------------------------------------------------
# BrowserSseNotifier
# ---------------------------------------------------------------------------


class BrowserSseNotifier:
    """Browser push notification channel via Server-Sent Events (SSE).

    Implements :class:`fis_monitor.domain.interfaces.Notifier` Protocol.

    **Design:**
    * Publishes ``SseLotNew`` events to the EventBus.
    * All connected browser clients subscribe to the bus and receive events
      via SSE (rendered as HTML fragments via ``fragment_template``).
    * ``test()`` is a no-op — the channel is push-only with no external
      endpoint to verify.

    **Thread-safety:**
    EventBus (``infra/sse/bus.py::ThreadEventBus``) uses ``queue.Queue``
    internally and is thread-safe. The NotifierDispatcher calls ``send()``
    in a dedicated thread pool.

    **Overflow handling (ADR-008):**
    When the per-subscriber queue reaches capacity (``maxsize=100``), the
    EventBus drops events from the tail (oldest UX events are sacrificed;
    the database remains the source of truth). This notifier gracefully
    logs the exception and returns success — the Dispatcher treats it as
    non-retryable so it doesn't spam the retry queue.
    """

    # --- Notifier Protocol ClassVars ---
    channel_id: ClassVar[str] = "browser"
    display_name: ClassVar[str] = "Browser (SSE)"
    description: ClassVar[str] = (
        "Push notifications to connected browser tabs via Server-Sent Events"
    )
    config_schema: ClassVar[type[NotifierConfig]] = BrowserNotifierConfig
    recipient_label: ClassVar[str] = "Browser tab"
    recipient_placeholder: ClassVar[str] = "(all connected tabs)"

    def __init__(self, event_bus: EventBus) -> None:
        """Initialize the browser notifier with an EventBus dependency.

        Args:
            event_bus: The EventBus Protocol instance (typically ThreadEventBus).
        """
        self._bus = event_bus

    # ------------------------------------------------------------------
    # Public API — Notifier Protocol
    # ------------------------------------------------------------------

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        """Publish a lot notification to the EventBus (broadcast to all tabs).

        The ``recipient`` parameter is ignored — browser SSE broadcasts to
        all connected clients. The EventBus handles per-subscriber queuing
        and drop-from-tail overflow.

        Args:
            lot: The lot DTO to publish.
            recipient: Ignored (recipient is always all connected tabs).

        Returns:
            NotifyResult with ok=True (event accepted or dropped gracefully).
            If the bus overflows, returns ok=True with detail="dropped (bus overflow)"
            and retryable=False, so the Dispatcher treats it as terminal and does
            not retry (per ADR-008 — the database is the source of truth).
        """
        try:
            self._bus.publish(SseLotNew(lot=lot, fragment_template="poster"))
            return NotifyResult(ok=True, detail="published", retryable=False)
        except Exception:
            # Broad Exception catch: covers queue.Full (overflow), any
            # unforeseen issues during publish. Log as warning and return
            # graceful no-op so the Dispatcher doesn't retry.
            logger.warning(
                "EventBus publish failed; lot will be available via database",
                exc_info=True,
                extra={"lot_id": lot.id, "channel_id": self.channel_id},
            )
            return NotifyResult(
                ok=True,
                detail="dropped (bus overflow)",
                retryable=False,
            )

    def test(self, recipient: str) -> NotifyResult:
        """Test the browser channel (no-op; the channel is push-only).

        Browser SSE has no external endpoint to verify — it is purely
        internal server-to-client streaming. This method always returns
        success without publishing anything.

        Args:
            recipient: Ignored.

        Returns:
            NotifyResult indicating the channel is push-only and requires
            no external test.
        """
        return NotifyResult(
            ok=True,
            detail="browser channel is push-only; no test send required",
            retryable=False,
        )
