"""Unit tests for OnboardingService — server-side FSM with guards.

Fakes:
- FakeSettingsRepository: in-memory dict, tracks all method calls.
- FakeConfigSource: returns a fixed Settings snapshot.

All fake methods are invoked in at least one test (not just isinstance checked).
"""

from __future__ import annotations

import pytest

from fis_monitor.domain.errors import InvalidTransitionError
from fis_monitor.domain.models import (
    EmailConfig,
    NotificationsConfig,
    OnboardingState,
    Settings,
)
from fis_monitor.services.onboarding import (
    KEY_EMAIL_SKIPPED,
    KEY_SMTP_TEST_OK,
    KEY_TEST_EMAIL_OK,
    OnboardingService,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSettingsRepository:
    """In-memory SettingsRepository for unit tests.

    Tracks state in ``_store`` (str key→str value) and ``_onboarding``.
    """

    def __init__(
        self,
        *,
        onboarding_state: OnboardingState = OnboardingState.NOT_STARTED,
        store: dict[str, str] | None = None,
    ) -> None:
        self._onboarding = onboarding_state
        self._store: dict[str, str] = store or {}
        # call tracking
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str]] = []
        self.get_onboarding_calls: int = 0
        self.set_onboarding_calls: list[OnboardingState] = []

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))
        self._store[key] = value

    def get_onboarding(self) -> OnboardingState:
        self.get_onboarding_calls += 1
        return self._onboarding

    def set_onboarding(self, st: OnboardingState) -> None:
        self.set_onboarding_calls.append(st)
        self._onboarding = st


class FakeConfigSource:
    """Returns a fixed Settings snapshot."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.current_calls: int = 0

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb):  # type: ignore[override]
        raise NotImplementedError("subscribe not used in unit tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    regions: list[int] | None = None,
    recipients: list[str] | None = None,
) -> Settings:
    """Build a minimal Settings with the given overrides."""
    if regions is None:
        regions = []
    email = EmailConfig(recipients=recipients or [])
    notifications = NotificationsConfig(email=email)
    return Settings(regions=regions, notifications=notifications)


def _service(
    *,
    state: OnboardingState = OnboardingState.NOT_STARTED,
    store: dict[str, str] | None = None,
    regions: list[int] | None = None,
    recipients: list[str] | None = None,
) -> tuple[OnboardingService, FakeSettingsRepository, FakeConfigSource]:
    repo = FakeSettingsRepository(onboarding_state=state, store=store)
    cfg = FakeConfigSource(_settings(regions=regions, recipients=recipients))
    svc = OnboardingService(settings_repo=repo, config_source=cfg)
    return svc, repo, cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCurrent:
    def test_current_default_not_started(self) -> None:
        svc, repo, _ = _service()
        assert svc.current() == OnboardingState.NOT_STARTED
        assert repo.get_onboarding_calls >= 1


class TestAdvanceLegalTransitions:
    def test_not_started_to_regions_set(self) -> None:
        svc, repo, cfg = _service(state=OnboardingState.NOT_STARTED, regions=[1, 2])
        svc.advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
        assert repo._onboarding == OnboardingState.REGIONS_SET
        # verify set_onboarding was actually called
        assert OnboardingState.REGIONS_SET in repo.set_onboarding_calls
        # verify config_source.current() was called for guard
        assert cfg.current_calls >= 1

    def test_regions_set_to_smtp_configured_via_smtp_test_ok(self) -> None:
        svc, repo, _ = _service(
            state=OnboardingState.REGIONS_SET,
            store={KEY_SMTP_TEST_OK: "true"},
        )
        svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
        assert repo._onboarding == OnboardingState.SMTP_CONFIGURED
        assert OnboardingState.SMTP_CONFIGURED in repo.set_onboarding_calls
        # get() was called for the smtp key
        assert KEY_SMTP_TEST_OK in repo.get_calls

    def test_smtp_configured_to_recipients_set(self) -> None:
        svc, repo, cfg = _service(
            state=OnboardingState.SMTP_CONFIGURED,
            recipients=["user@example.com"],
        )
        svc.advance(OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET)
        assert repo._onboarding == OnboardingState.RECIPIENTS_SET
        assert OnboardingState.RECIPIENTS_SET in repo.set_onboarding_calls
        assert cfg.current_calls >= 1

    def test_recipients_set_to_completed_via_test_email_ok(self) -> None:
        svc, repo, _ = _service(
            state=OnboardingState.RECIPIENTS_SET,
            store={KEY_TEST_EMAIL_OK: "true"},
        )
        svc.advance(OnboardingState.RECIPIENTS_SET, OnboardingState.COMPLETED)
        assert repo._onboarding == OnboardingState.COMPLETED
        assert OnboardingState.COMPLETED in repo.set_onboarding_calls
        assert KEY_TEST_EMAIL_OK in repo.get_calls


class TestAdvanceIllegalTransitions:
    def test_advance_skip_state_raises(self) -> None:
        """not_started → smtp_configured skips regions_set — must raise."""
        svc, _, _ = _service(
            state=OnboardingState.NOT_STARTED,
            store={KEY_SMTP_TEST_OK: "true"},
        )
        with pytest.raises(InvalidTransitionError) as exc_info:
            svc.advance(OnboardingState.NOT_STARTED, OnboardingState.SMTP_CONFIGURED)
        err = exc_info.value
        assert err.current_state == OnboardingState.NOT_STARTED.value
        assert err.requested_from == OnboardingState.NOT_STARTED.value
        assert err.requested_to == OnboardingState.SMTP_CONFIGURED.value

    def test_advance_with_failed_guard_raises(self) -> None:
        """regions_set → smtp_configured without smtp_test_ok and without email_skipped."""
        svc, _, _ = _service(state=OnboardingState.REGIONS_SET)
        with pytest.raises(InvalidTransitionError):
            svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)

    def test_advance_concurrent_transition_raises(self) -> None:
        """current() returns already-advanced state; advance with old from_state raises."""
        # Simulate: another request already advanced to REGIONS_SET
        svc, _repo, _ = _service(state=OnboardingState.REGIONS_SET, regions=[1])
        # caller still thinks it's NOT_STARTED
        with pytest.raises(InvalidTransitionError) as exc_info:
            svc.advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
        err = exc_info.value
        # current is REGIONS_SET (already advanced), requested_from was NOT_STARTED
        assert err.current_state == OnboardingState.REGIONS_SET.value
        assert err.requested_from == OnboardingState.NOT_STARTED.value

    def test_advance_idempotent_raises(self) -> None:
        """Already in REGIONS_SET; re-advance NOT_STARTED→REGIONS_SET raises."""
        svc, _, _ = _service(state=OnboardingState.REGIONS_SET, regions=[1])
        with pytest.raises(InvalidTransitionError) as exc_info:
            svc.advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
        err = exc_info.value
        # current is to_state (already there); caller gets a clear error
        assert err.current_state == OnboardingState.REGIONS_SET.value
        assert err.requested_from == OnboardingState.NOT_STARTED.value
        assert err.requested_to == OnboardingState.REGIONS_SET.value


class TestEmailSkipped:
    def test_email_skipped_bypasses_smtp_guard(self) -> None:
        """email_skipped=true allows regions_set→smtp_configured without smtp_test."""
        svc, repo, _ = _service(
            state=OnboardingState.REGIONS_SET,
            store={KEY_EMAIL_SKIPPED: "true"},
        )
        svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)
        assert repo._onboarding == OnboardingState.SMTP_CONFIGURED
        # verify get() was called for KEY_EMAIL_SKIPPED
        assert KEY_EMAIL_SKIPPED in repo.get_calls

    def test_skip_email_allowed_in_smtp_configured(self) -> None:
        svc, repo, _ = _service(state=OnboardingState.SMTP_CONFIGURED)
        svc.skip_email()
        assert (KEY_EMAIL_SKIPPED, "true") in repo.set_calls

    def test_skip_email_allowed_in_recipients_set(self) -> None:
        svc, repo, _ = _service(state=OnboardingState.RECIPIENTS_SET)
        svc.skip_email()
        assert (KEY_EMAIL_SKIPPED, "true") in repo.set_calls

    def test_skip_email_only_allowed_in_smtp_configured_or_recipients_set(self) -> None:
        """skip_email raises in all other states."""
        disallowed_states = [
            OnboardingState.NOT_STARTED,
            OnboardingState.REGIONS_SET,
            OnboardingState.COMPLETED,
        ]
        for state in disallowed_states:
            svc, _, _ = _service(state=state)
            with pytest.raises(InvalidTransitionError, match="skip_email"):
                svc.skip_email()


class TestUrlForCurrentStep:
    @pytest.mark.parametrize(
        ("state", "expected_url"),
        [
            (OnboardingState.NOT_STARTED, "/onboarding/regions"),
            (OnboardingState.REGIONS_SET, "/onboarding/smtp"),
            (OnboardingState.SMTP_CONFIGURED, "/onboarding/recipients"),
            (OnboardingState.RECIPIENTS_SET, "/onboarding/test-email"),
            (OnboardingState.COMPLETED, "/"),
        ],
    )
    def test_url_for_current_step(self, state: OnboardingState, expected_url: str) -> None:
        svc, repo, _ = _service(state=state)
        assert svc.url_for_current_step() == expected_url
        # get_onboarding was called (url_for_current_step → current())
        assert repo.get_onboarding_calls >= 1


class TestFakeMethodCoverage:
    """Verify all fake-repo methods participate in real test logic (not just isinstance)."""

    def test_all_repo_methods_called(self) -> None:
        """Single scenario that touches get, set, get_onboarding, set_onboarding."""
        repo = FakeSettingsRepository(
            onboarding_state=OnboardingState.REGIONS_SET,
            store={KEY_SMTP_TEST_OK: "true"},
        )
        cfg = FakeConfigSource(_settings(regions=[1]))
        svc = OnboardingService(settings_repo=repo, config_source=cfg)

        # get_onboarding: current() → advance() step 1
        # get: can_advance → _guard_satisfied → repo.get(KEY_SMTP_TEST_OK)
        # set_onboarding: advance() step 3
        svc.advance(OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED)

        assert repo.get_onboarding_calls >= 1
        assert KEY_SMTP_TEST_OK in repo.get_calls
        assert OnboardingState.SMTP_CONFIGURED in repo.set_onboarding_calls

        # set: skip_email
        svc2 = OnboardingService(
            settings_repo=FakeSettingsRepository(
                onboarding_state=OnboardingState.SMTP_CONFIGURED,
            ),
            config_source=cfg,
        )
        repo2 = svc2._repo  # type: ignore[attr-defined]
        svc2.skip_email()
        assert len(repo2.set_calls) >= 1
        assert repo2.set_calls[0] == (KEY_EMAIL_SKIPPED, "true")
