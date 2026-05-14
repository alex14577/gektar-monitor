"""Unit tests for DndService.

Coverage:
  (a) is_active() → False when no key present.
  (b) is_active() → True when now < dnd_until.
  (c) is_active() → False when now >= dnd_until (window expired).
  (d) set_dnd_until() stores correct ISO UTC timestamp.
  (e) Anti-mock: all FakeSettingsRepository methods exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fis_monitor.services.dnd import DndService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSettingsRepository:
    """In-memory KV store satisfying the SettingsRepository Protocol subset
    used by DndService (get/set only)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    # Protocol completeness — not used by DndService.
    def get_onboarding(self) -> object:  # pragma: no cover
        return None

    def set_onboarding(self, st: object) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Anti-mock: all FakeSettingsRepository methods used by the SUT
# ---------------------------------------------------------------------------


def test_fake_settings_repository_all_methods() -> None:
    """Exercise get() and set() to catch runtime API bugs in the fake."""
    repo = FakeSettingsRepository()
    assert repo.get("missing") is None
    repo.set("k", "v")
    assert repo.get("k") == "v"


# ---------------------------------------------------------------------------
# (a) is_active → False when no key present
# ---------------------------------------------------------------------------


class TestIsActiveNoKey:
    def test_returns_false_when_no_dnd_recorded(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        assert svc.is_active(now) is False

    def test_until_returns_none_when_no_dnd_recorded(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        assert svc.until(now) is None


# ---------------------------------------------------------------------------
# (b) is_active → True when now < dnd_until
# ---------------------------------------------------------------------------


class TestIsActiveWithinWindow:
    def test_returns_true_when_inside_window(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        svc.set_dnd_until(base, minutes=60)

        # Query 30 minutes later — inside the 60-min window.
        now_later = base + timedelta(minutes=30)
        assert svc.is_active(now_later) is True

    def test_returns_true_just_before_expiry(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

        svc.set_dnd_until(base, minutes=60)

        almost_expired = base + timedelta(minutes=60) - timedelta(seconds=1)
        assert svc.is_active(almost_expired) is True


# ---------------------------------------------------------------------------
# (c) is_active → False when window has expired
# ---------------------------------------------------------------------------


class TestIsActiveExpired:
    def test_returns_false_exactly_at_expiry(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

        svc.set_dnd_until(base, minutes=60)

        exactly_at = base + timedelta(minutes=60)
        assert svc.is_active(exactly_at) is False

    def test_returns_false_after_expiry(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

        svc.set_dnd_until(base, minutes=60)

        past_expiry = base + timedelta(minutes=61)
        assert svc.is_active(past_expiry) is False


# ---------------------------------------------------------------------------
# (d) set_dnd_until stores correct timestamp
# ---------------------------------------------------------------------------


class TestSetDndUntilStoresTimestamp:
    def test_stores_now_plus_minutes(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 10, 30, 0, tzinfo=UTC)

        svc.set_dnd_until(base, minutes=90)

        raw = repo.get("dnd_until")
        assert raw is not None, "Expected dnd_until to be set in repo"

        stored = datetime.fromisoformat(raw)
        expected = base + timedelta(minutes=90)
        delta = abs((stored - expected).total_seconds())
        assert delta < 1, f"Stored ts {stored!r} deviates from expected {expected!r} by {delta}s"

    def test_raises_on_zero_minutes(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

        try:
            svc.set_dnd_until(base, minutes=0)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError for minutes=0")

    def test_raises_on_negative_minutes(self) -> None:
        repo = FakeSettingsRepository()
        svc = DndService(settings_repo=repo)
        base = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

        try:
            svc.set_dnd_until(base, minutes=-1)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError for minutes=-1")
