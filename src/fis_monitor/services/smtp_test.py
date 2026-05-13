"""SmtpTestService — send a test email and record the outcome in settings.

Delegates the actual send to the injected ``Notifier`` (email notifier from
the registry), then updates ``SettingsRepository`` with the outcome so that:
  - The onboarding FSM knows whether the test email step was completed
    (``onboarding_test_email_ok=true``).
  - The UI can surface a freshness indicator (``smtp_test_last_result_ok``,
    ``smtp_test_last_result_at``).

SMTP / STARTTLS / DNS logic is NOT duplicated here — that all lives in
``SmtpEmailNotifier`` (ADR-015, ADR-021).  This service only interprets the
``NotifyResult`` and writes the appropriate flags.

No HMAC on MVP per ADR-018 known-limitation R3-M10.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.interfaces import Clock, Notifier, SettingsRepository
from fis_monitor.domain.models import LotPublicDTO, NotifyResult


class SmtpTestService:
    """Send a test email and record success/failure in the settings store.

    Responsibilities (SRP):
    - Call ``notifier.send(test_lot, recipient)`` to exercise the full SMTP
      path (including STARTTLS, DNS, auth).
    - Write outcome flags to ``SettingsRepository`` so onboarding and UI can
      react without re-querying the notifier.

    The notifier is expected to be the email notifier from the registry,
    already wired with SMTP credentials at construction time (passed in from
    the composition root).

    Args:
        notifier:      Email ``Notifier`` instance.
        settings_repo: Key/value repository for onboarding and test-result flags.
        clock:         Injected time source (deterministic in tests).
    """

    def __init__(
        self,
        notifier: Notifier,
        settings_repo: SettingsRepository,
        clock: Clock,
    ) -> None:
        self._notifier = notifier
        self._settings_repo = settings_repo
        self._clock = clock

    def test_send(self, test_lot: LotPublicDTO, recipient: str) -> NotifyResult:
        """Send a test notification and persist the outcome.

        Calls ``notifier.send(test_lot, recipient)`` synchronously.

        On success (``result.ok=True``):
            - ``smtp_test_last_result_ok`` ← ``"true"``
            - ``smtp_test_last_result_at``  ← ISO-8601 UTC timestamp
            - ``onboarding_test_email_ok``  ← ``"true"``

        On failure (``result.ok=False``):
            - ``smtp_test_last_result_ok`` ← ``"false"`` (clears any previous
              ``"true"`` so stale success is never shown after a fresh failure)
            - ``smtp_test_last_result_at``  ← ISO-8601 UTC timestamp
            - ``onboarding_test_email_ok`` is NOT set to ``"true"`` (a failed
              test does not advance the onboarding step).

        Returns:
            The ``NotifyResult`` from ``notifier.send()``.  Caller may inspect
            ``result.ok`` and ``result.detail`` to surface a UI message.
        """
        result = self._notifier.send(test_lot, recipient)

        now_iso = _to_iso(self._clock.now())

        if result.ok:
            self._settings_repo.set("smtp_test_last_result_ok", "true")
            self._settings_repo.set("smtp_test_last_result_at", now_iso)
            self._settings_repo.set("onboarding_test_email_ok", "true")
        else:
            # Overwrite any previously stored "true" so the UI does not show a
            # stale success badge after a fresh failure.
            self._settings_repo.set("smtp_test_last_result_ok", "false")
            self._settings_repo.set("smtp_test_last_result_at", now_iso)
            # onboarding_test_email_ok is intentionally NOT set to "true" here.

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime) -> str:
    """Return an ISO-8601 UTC string (always with Z suffix for unambiguity).

    If ``dt`` is naive it is assumed to be UTC (defence-in-depth — ``Clock``
    implementations MUST return aware UTC datetimes per the interface contract).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
