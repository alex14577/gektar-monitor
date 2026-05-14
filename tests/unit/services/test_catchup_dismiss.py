"""Unit tests for CatchupDismissService.

Coverage:
  (a) is_dismissed() → False when no key present.
  (b) is_dismissed() → True when now < dismissed_until.
  (c) is_dismissed() → False when now >= dismissed_until (window expired).
  (d) dismiss() stores the correct timestamp (now + hours).
  (e) Fake covers all methods exercised by the service (anti-mock invariant).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fis_monitor.services.catchup_dismiss import CatchupDismissService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSettingsRepository:
    """In-memory KV store satisfying the SettingsRepository Protocol subset
    used by CatchupDismissService (get/set only)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    # onboarding helpers — not used by CatchupDismissService, present for
    # Protocol completeness; exercised in test_fake_all_methods below.
    def get_onboarding(self) -> object:  # pragma: no cover
        return None

    def set_onboarding(self, st: object) -> None:  # pragma: no cover
        pass


class FakeClock:
    """Deterministic clock returning a fixed UTC datetime."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now


# ---------------------------------------------------------------------------
# Anti-mock: all methods of FakeSettingsRepository used by the SUT
# ---------------------------------------------------------------------------


def test_fake_settings_repository_all_methods() -> None:
    """Exercise get() and set() so runtime API bugs in the fake are caught."""
    repo = FakeSettingsRepository()
    assert repo.get("missing") is None
    repo.set("k", "v")
    assert repo.get("k") == "v"


# ---------------------------------------------------------------------------
# (a) is_dismissed → False when no key present
# ---------------------------------------------------------------------------


class TestIsDismissedNoKey:
    def test_returns_false_when_no_dismissal_recorded(self) -> None:
        repo = FakeSettingsRepository()
        clock = FakeClock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC))
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        assert svc.is_dismissed(now=clock.now()) is False


# ---------------------------------------------------------------------------
# (b) is_dismissed → True when now < dismissed_until
# ---------------------------------------------------------------------------


class TestIsDismissedWithinWindow:
    def test_returns_true_when_inside_window(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=24)

        # Query 1 hour after dismissal — still inside the 24h window.
        now_later = base + timedelta(hours=1)
        assert svc.is_dismissed(now=now_later) is True

    def test_returns_true_just_before_expiry(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=24)

        # One second before expiry.
        almost_expired = base + timedelta(hours=24) - timedelta(seconds=1)
        assert svc.is_dismissed(now=almost_expired) is True


# ---------------------------------------------------------------------------
# (c) is_dismissed → False when window has expired
# ---------------------------------------------------------------------------


class TestIsDismissedExpired:
    def test_returns_false_exactly_at_expiry(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=24)

        # Query exactly at now + 24h — not strictly less than, so False.
        exactly_at = base + timedelta(hours=24)
        assert svc.is_dismissed(now=exactly_at) is False

    def test_returns_false_after_expiry(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=24)

        # Query one second past expiry.
        past_expiry = base + timedelta(hours=24, seconds=1)
        assert svc.is_dismissed(now=past_expiry) is False


# ---------------------------------------------------------------------------
# (d) dismiss() stores correct timestamp
# ---------------------------------------------------------------------------


class TestDismissStoresTimestamp:
    def test_dismiss_sets_now_plus_hours(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 10, 30, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=24)

        raw = repo.get("catchup_dismissed_until")
        assert raw is not None, "Expected catchup_dismissed_until to be set"

        stored = datetime.fromisoformat(raw)
        expected = base + timedelta(hours=24)
        delta = abs((stored - expected).total_seconds())
        assert delta < 1, f"Stored ts {stored!r} deviates from expected {expected!r} by {delta}s"

    def test_dismiss_custom_hours(self) -> None:
        repo = FakeSettingsRepository()
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
        clock = FakeClock(base)
        svc = CatchupDismissService(state_repo=repo, clock=clock)

        svc.dismiss(now=base, hours=48)

        raw = repo.get("catchup_dismissed_until")
        assert raw is not None
        stored = datetime.fromisoformat(raw)
        expected = base + timedelta(hours=48)
        assert abs((stored - expected).total_seconds()) < 1
