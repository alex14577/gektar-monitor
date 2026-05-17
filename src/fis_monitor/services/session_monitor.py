"""SessionMonitor service — Layer 3.

Probes the target site to detect whether the stored session cookies are still
valid, and publishes ``SseSessionExpired`` on the critical EventBus channel
when the session is found to have expired.

Design rationale: see docs/decisions/ADR-046-session-monitor-combined-probe-and-publish.md
"""

from __future__ import annotations

import logging

from fis_monitor.domain.interfaces import Clock, EventBus, HttpClient
from fis_monitor.domain.models import SessionStatus, SseSessionExpired

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level probe constants
# ---------------------------------------------------------------------------
_PROBE_PATH: str = "/cabinet/"
"""Relative path of the auth-gated endpoint used to detect session validity.

GET /cabinet/ returns 200 when the session is active; it redirects (302) to
/login* when the session has expired, resulting in a ``final_url`` that
contains ``/login``.
"""

_LOGIN_REDIRECT_FRAGMENT: str = "/login"
"""Substring that ``HttpResponse.final_url`` contains after a session-expiry
redirect.  Detection is intentionally loose (substring, not exact-match) to
tolerate minor path variations in the redirect chain.
"""


class SessionMonitor:
    """Probe the target site and publish ``SseSessionExpired`` on expiry.

    Usage::

        status = session_monitor.check()   # ACTIVE or EXPIRED

    ``check()`` is stateless — callers (periodic scheduler, health endpoint)
    are responsible for polling cadence.

    EXPIRING detection is not implemented in this version; no reliable HTTP-only
    signal is available.  A follow-up bd-task has been filed to add cookie-expiry
    parsing or body-marker heuristics.
    """

    def __init__(
        self,
        *,
        http_client: HttpClient,
        event_bus: EventBus,
        clock: Clock,
        base_url: str,
    ) -> None:
        self._http_client = http_client
        self._event_bus = event_bus
        self._clock = clock
        self._base_url = base_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self) -> SessionStatus:
        """Probe the target site and return the current ``SessionStatus``.

        Detection logic:
        - ``final_url`` contains ``/login`` → EXPIRED (session cookie invalid,
          server redirected to login page).  Publishes ``SseSessionExpired``
          before returning.
        - HTTP 200 and no login redirect → ACTIVE.
        - Any other status (5xx, connection-level non-2xx) → EXPIRED (fail-safe).
          A warning is logged but the event is still published to unblock the
          UI / email alert path.

        OSError and other network-level exceptions are intentionally NOT caught
        here — the caller's retry policy is responsible for handling transient
        connectivity failures.
        """
        resp = self._http_client.get(self._base_url + _PROBE_PATH)

        if _LOGIN_REDIRECT_FRAGMENT in resp.final_url:
            _log.info(
                "session_monitor.check: login redirect detected — session expired",
                extra={"final_url": resp.final_url},
            )
            self._event_bus.publish(SseSessionExpired(timestamp=self._clock.now()))
            return SessionStatus.EXPIRED

        if resp.status == 200:
            return SessionStatus.ACTIVE

        # Unexpected status (e.g. 5xx from upstream) — treat as expired (fail-safe).
        _log.warning(
            "session_monitor.check: unexpected HTTP status %s — treating as EXPIRED (fail-safe)",
            resp.status,
            extra={"final_url": resp.final_url},
        )
        self._event_bus.publish(SseSessionExpired(timestamp=self._clock.now()))
        return SessionStatus.EXPIRED
