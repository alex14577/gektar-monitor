"""Unit tests for SmtpTestService.

Contract under test:
  1. On ok=True: smtp_test_last_result_ok=true,
                  smtp_test_last_result_at=ISO, onboarding_test_email_ok=true.
  2. On ok=False: smtp_test_last_result_ok=false (overwrites stale true),
                   smtp_test_last_result_at=ISO, onboarding_test_email_ok NOT set to true.
  3. Returns the NotifyResult as-is.
  4. FakeNotifier.send is actually called with the expected arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from fis_monitor.domain.interfaces import Notifier
from fis_monitor.domain.models import (
    LotPublicDTO,
    NotifierConfig,
    NotifyResult,
)
from fis_monitor.services.smtp_test import SmtpTestService
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNotifier:
    """Notifier fake that returns a preconfigured result and records calls."""

    channel_id: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Email (fake)"
    description: ClassVar[str] = "Fake email notifier for tests"
    config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
    recipient_label: ClassVar[str] = "Email address"
    recipient_placeholder: ClassVar[str] = "user@example.com"

    def __init__(self, *, result: NotifyResult) -> None:
        self._result = result
        self.send_calls: list[tuple[LotPublicDTO, str]] = []
        self.test_calls: list[str] = []

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        self.send_calls.append((lot, recipient))
        return self._result

    def test(self, recipient: str) -> NotifyResult:
        self.test_calls.append(recipient)
        return self._result


# Verify FakeNotifier satisfies the Notifier Protocol at runtime.
_probe = FakeNotifier(result=NotifyResult(ok=True, detail="ok", retryable=False))
assert isinstance(_probe, Notifier)


class FakeSettingsRepository:
    """In-memory key/value settings store."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))
        self._store[key] = value

    def get_onboarding(self):  # type: ignore[return]
        pass

    def set_onboarding(self, st) -> None:  # type: ignore[override]
        pass


class FakeClock:
    """Fixed-time clock."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._fixed

    def monotonic(self) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_lot() -> LotPublicDTO:
    base = make_lot()
    return LotPublicDTO(
        **base.model_dump(),
        age_seconds=3600,
        tier="match",
        freshness="hot",
    )


@pytest.fixture()
def fixed_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def ok_result() -> NotifyResult:
    return NotifyResult(ok=True, detail="sent", retryable=False)


@pytest.fixture()
def fail_result() -> NotifyResult:
    return NotifyResult(ok=False, detail="auth failure", retryable=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTestSendOk:
    def test_test_send_ok_sets_smtp_test_keys(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        ok_result: NotifyResult,
    ) -> None:
        """ok=True → all three keys set in settings repo."""
        notifier = FakeNotifier(result=ok_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)

        svc.test_send(test_lot, "user@example.com")

        assert repo.get("smtp_test_last_result_ok") == "true"
        assert repo.get("onboarding_test_email_ok") == "true"
        at_value = repo.get("smtp_test_last_result_at")
        assert at_value is not None
        # Must be a valid ISO-8601 string matching the clock's fixed time.
        assert "2026-01-15" in at_value

    def test_test_send_ok_timestamp_matches_clock(
        self,
        test_lot: LotPublicDTO,
        ok_result: NotifyResult,
    ) -> None:
        """The ISO timestamp stored must equal the clock's now()."""
        fixed_dt = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
        clock = FakeClock(fixed=fixed_dt)
        notifier = FakeNotifier(result=ok_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=clock)

        svc.test_send(test_lot, "u@x.com")

        at_value = repo.get("smtp_test_last_result_at")
        assert at_value is not None
        # Parse back to verify round-trip.
        parsed = datetime.fromisoformat(at_value)
        assert parsed.replace(tzinfo=UTC) == fixed_dt.replace(tzinfo=UTC)


class TestTestSendFail:
    def test_test_send_fail_clears_smtp_test_ok(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        fail_result: NotifyResult,
    ) -> None:
        """ok=False → smtp_test_last_result_ok=false; onboarding flag NOT set true."""
        notifier = FakeNotifier(result=fail_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)

        svc.test_send(test_lot, "user@example.com")

        assert repo.get("smtp_test_last_result_ok") == "false"
        # onboarding_test_email_ok must NOT be "true" after failure.
        assert repo.get("onboarding_test_email_ok") != "true"

    def test_test_send_fail_overwrites_previous_true(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        ok_result: NotifyResult,
        fail_result: NotifyResult,
    ) -> None:
        """Stale 'true' is overwritten to 'false' on next failure."""
        repo = FakeSettingsRepository()
        # First call succeeds.
        svc_ok = SmtpTestService(
            notifier=FakeNotifier(result=ok_result),
            settings_repo=repo,
            clock=fixed_clock,
        )
        svc_ok.test_send(test_lot, "u@x.com")
        assert repo.get("smtp_test_last_result_ok") == "true"

        # Second call fails — stale 'true' must be overwritten.
        svc_fail = SmtpTestService(
            notifier=FakeNotifier(result=fail_result),
            settings_repo=repo,
            clock=fixed_clock,
        )
        svc_fail.test_send(test_lot, "u@x.com")
        assert repo.get("smtp_test_last_result_ok") == "false"

    def test_test_send_fail_still_writes_timestamp(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        fail_result: NotifyResult,
    ) -> None:
        """Even on failure, smtp_test_last_result_at is updated."""
        notifier = FakeNotifier(result=fail_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)

        svc.test_send(test_lot, "u@x.com")

        assert repo.get("smtp_test_last_result_at") is not None


class TestTestSendReturnValue:
    def test_test_send_returns_notify_result(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        ok_result: NotifyResult,
    ) -> None:
        """test_send must return exactly the NotifyResult from the notifier."""
        notifier = FakeNotifier(result=ok_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)

        returned = svc.test_send(test_lot, "u@x.com")

        assert returned is ok_result

    def test_test_send_returns_fail_result(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        fail_result: NotifyResult,
    ) -> None:
        """test_send must return the failure NotifyResult unchanged."""
        notifier = FakeNotifier(result=fail_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)

        returned = svc.test_send(test_lot, "u@x.com")

        assert returned is fail_result
        assert returned.ok is False
        assert returned.detail == "auth failure"


class TestFakeNotifierActuallyCalled:
    def test_fake_notifier_send_actually_called(
        self,
        test_lot: LotPublicDTO,
        fixed_clock: FakeClock,
        ok_result: NotifyResult,
    ) -> None:
        """FakeNotifier.send must be invoked with the correct arguments."""
        notifier = FakeNotifier(result=ok_result)
        repo = FakeSettingsRepository()
        svc = SmtpTestService(notifier=notifier, settings_repo=repo, clock=fixed_clock)
        recipient = "recipient@example.com"

        svc.test_send(test_lot, recipient)

        assert len(notifier.send_calls) == 1, "send must be called exactly once"
        called_lot, called_recipient = notifier.send_calls[0]
        assert called_lot is test_lot
        assert called_recipient == recipient

    def test_fake_notifier_test_method_callable(self) -> None:
        """FakeNotifier.test() is defined and callable — ensures the fake fully
        implements the Notifier Protocol (all methods exercised, not just send)."""
        result = NotifyResult(ok=True, detail="test ok", retryable=False)
        notifier = FakeNotifier(result=result)

        ret = notifier.test("u@x.com")

        assert ret is result
        assert notifier.test_calls == ["u@x.com"]

    def test_fake_settings_repo_set_and_get(self) -> None:
        """FakeSettingsRepository.get and .set both exercised."""
        repo = FakeSettingsRepository()
        assert repo.get("missing_key") is None

        repo.set("foo", "bar")
        assert repo.get("foo") == "bar"
        assert ("foo", "bar") in repo.set_calls
