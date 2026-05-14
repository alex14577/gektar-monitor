"""OnboardingService — server-side FSM with guards (ADR-018).

State machine:
    not_started → regions_set → smtp_configured → recipients_set → completed

Guards are pure reads from SettingsRepository / ConfigSource — no side effects.
``advance()`` re-reads current state before writing (concurrent-safe re-read).

See docs/onboarding.md and docs/decisions/ADR-018-onboarding-fsm-server-enforced.md.
"""

from __future__ import annotations

from fis_monitor.domain.errors import InvalidTransitionError
from fis_monitor.domain.interfaces import ConfigSource, SettingsRepository
from fis_monitor.domain.models import OnboardingState

# ---------------------------------------------------------------------------
# Module-level key constants — single source of truth for state.db key names
# ---------------------------------------------------------------------------
KEY_EMAIL_SKIPPED = "email_skipped"
KEY_SMTP_TEST_OK = "smtp_test_last_result_ok"
KEY_TEST_EMAIL_OK = "onboarding_test_email_ok"

# ---------------------------------------------------------------------------
# Valid sequential transition chain
# ---------------------------------------------------------------------------
_NEXT_STATE: dict[OnboardingState, OnboardingState] = {
    OnboardingState.NOT_STARTED: OnboardingState.REGIONS_SET,
    OnboardingState.REGIONS_SET: OnboardingState.SMTP_CONFIGURED,
    OnboardingState.SMTP_CONFIGURED: OnboardingState.RECIPIENTS_SET,
    OnboardingState.RECIPIENTS_SET: OnboardingState.COMPLETED,
}

# ---------------------------------------------------------------------------
# State → UI URL mapping
# ---------------------------------------------------------------------------
_STATE_URL: dict[OnboardingState, str] = {
    OnboardingState.NOT_STARTED: "/onboarding/regions",
    OnboardingState.REGIONS_SET: "/onboarding/smtp",
    OnboardingState.SMTP_CONFIGURED: "/onboarding/recipients",
    OnboardingState.RECIPIENTS_SET: "/onboarding/test-email",
    OnboardingState.COMPLETED: "/",
}


class OnboardingService:
    """Implements the server-side onboarding FSM with guards.

    Invariants:
    - ``can_advance`` is a pure read — never writes.
    - ``advance`` re-reads current state inside itself before writing to
      protect against concurrent transitions (double-tab / retry scenario).
    - ``skip_email`` is only permitted in ``smtp_configured`` or
      ``recipients_set``; outside those states raises ``InvalidTransitionError``.

    See [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]]
    and [[onboarding]].
    """

    def __init__(
        self,
        settings_repo: SettingsRepository,
        config_source: ConfigSource,
    ) -> None:
        self._repo = settings_repo
        self._config = config_source

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current(self) -> OnboardingState:
        """Return the current onboarding state from state.db (default: NOT_STARTED)."""
        return self._repo.get_onboarding()

    def can_advance(
        self,
        from_state: OnboardingState,
        to_state: OnboardingState,
    ) -> bool:
        """Return True iff the transition from_state → to_state is legal.

        Pure read — checks guard conditions without writing anything.

        A transition is legal when:
        1. ``to_state`` is the immediate successor of ``from_state`` in the chain.
        2. The guard for ``to_state`` is satisfied (see docs/onboarding.md).
        """
        if _NEXT_STATE.get(from_state) != to_state:
            return False
        return self._guard_satisfied(to_state)

    def advance(
        self,
        from_state: OnboardingState,
        to_state: OnboardingState,
    ) -> None:
        """Attempt to move the FSM from ``from_state`` to ``to_state``.

        Steps (per contract):
        1. Re-read current() — concurrent-transition protection.
        2. Verify can_advance(from_state, to_state).
        3. Persist to_state via settings_repo.set_onboarding().

        Raises ``InvalidTransitionError`` on any violation.
        """
        curr = self._repo.get_onboarding()
        if curr != from_state:
            raise InvalidTransitionError(
                curr.value,
                from_state.value,
                to_state.value,
            )
        if not self.can_advance(from_state, to_state):
            raise InvalidTransitionError(
                curr.value,
                from_state.value,
                to_state.value,
            )
        self._repo.set_onboarding(to_state)

    def skip_email(self) -> None:
        """Set the email_skipped flag.

        Allowed once the user has reached the email-configuration phase:
        ``regions_set`` (step 2 — кнопка «Настроить позже» прямо на SMTP-форме),
        ``smtp_configured`` (step 3 — пропустить получателей), or
        ``recipients_set`` (step 4 — пропустить тест-email).
        Raises ``InvalidTransitionError`` in any other state.

        Note: extended from {smtp_configured, recipients_set} to also include
        regions_set per 0vn runtime fix — без этого «Настроить позже» на step 2
        упирался в chicken-and-egg: невозможно advance до smtp_configured без
        email_skipped, и невозможно поставить email_skipped до smtp_configured.
        """
        curr = self._repo.get_onboarding()
        allowed = {
            OnboardingState.REGIONS_SET,
            OnboardingState.SMTP_CONFIGURED,
            OnboardingState.RECIPIENTS_SET,
        }
        if curr not in allowed:
            raise InvalidTransitionError(
                curr.value,
                "regions_set|smtp_configured|recipients_set",
                "skip_email",
            )
        self._repo.set(KEY_EMAIL_SKIPPED, "true")

    def url_for_current_step(self) -> str:
        """Return the UI URL for the current onboarding step."""
        return _STATE_URL[self.current()]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_email_skipped(self) -> bool:
        return self._repo.get(KEY_EMAIL_SKIPPED) == "true"

    def _guard_satisfied(self, to_state: OnboardingState) -> bool:
        """Return True iff the guard condition for entering ``to_state`` is met.

        Guards (docs/onboarding.md):
        - REGIONS_SET:    len(settings.regions) > 0
        - SMTP_CONFIGURED: smtp_test_last_result_ok OR email_skipped
        - RECIPIENTS_SET: len(recipients) > 0 OR email_skipped
        - COMPLETED:      onboarding_test_email_ok OR email_skipped
        """
        settings = self._config.current()
        skipped = self._is_email_skipped()

        if to_state is OnboardingState.REGIONS_SET:
            return len(settings.regions) > 0

        if to_state is OnboardingState.SMTP_CONFIGURED:
            smtp_ok = self._repo.get(KEY_SMTP_TEST_OK) == "true"
            return smtp_ok or skipped

        if to_state is OnboardingState.RECIPIENTS_SET:
            has_recipients = len(settings.notifications.email.recipients) > 0
            return has_recipients or skipped

        if to_state is OnboardingState.COMPLETED:
            test_email_ok = self._repo.get(KEY_TEST_EMAIL_OK) == "true"
            return test_email_ok or skipped

        # NOT_STARTED is never a to_state in a legal advance
        return False
