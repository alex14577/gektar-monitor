"""Unit tests for ExplicitNotifierRegistry.

Coverage:
  1. register + get returns same instance.
  2. Duplicate channel_id raises RegistrationError.
  3. Non-Notifier object raises RegistrationError.
  4. get() for unknown channel_id raises KeyError.
  5. has() returns bool correctly.
  6. all() preserves registration order.
  7. Registered fake notifier send() is callable and returns NotifyResult.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from fis_monitor.domain.errors import RegistrationError
from fis_monitor.domain.interfaces import Notifier
from fis_monitor.domain.models import LotPublicDTO, NotifierConfig, NotifyResult
from fis_monitor.infra.notifiers.registry import ExplicitNotifierRegistry
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Fake Notifier fixture
# ---------------------------------------------------------------------------


def _make_fake_notifier_class(channel: str) -> type:
    """Create a fake Notifier class with the given channel_id."""

    class FakeNotifier:
        channel_id: ClassVar[str] = channel
        display_name: ClassVar[str] = f"Fake {channel}"
        description: ClassVar[str] = f"Fake notifier for channel {channel}."
        config_schema: ClassVar[type[NotifierConfig]] = NotifierConfig
        recipient_label: ClassVar[str] = "To"
        recipient_placeholder: ClassVar[str] = "user@example.com"

        def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
            return NotifyResult(ok=True, detail="", retryable=False)

        def test(self, recipient: str) -> NotifyResult:
            return NotifyResult(ok=True, detail="", retryable=False)

    FakeNotifier.__qualname__ = f"FakeNotifier_{channel}"
    return FakeNotifier


@pytest.fixture()
def fake_notifier_class() -> type:
    """A fresh FakeNotifier class with channel_id='fake'."""
    return _make_fake_notifier_class("fake")


@pytest.fixture()
def fake_notifier(fake_notifier_class: type) -> Notifier:
    """A single FakeNotifier instance."""
    return fake_notifier_class()


@pytest.fixture()
def registry() -> ExplicitNotifierRegistry:
    """A fresh ExplicitNotifierRegistry."""
    return ExplicitNotifierRegistry()


@pytest.fixture()
def lot_dto() -> LotPublicDTO:
    """A minimal LotPublicDTO for notifier send() calls."""
    base = make_lot()
    return LotPublicDTO(
        **base.model_dump(),
        age_seconds=60,
        tier="match",
        freshness="hot",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_and_get_returns_same_instance(
    registry: ExplicitNotifierRegistry,
    fake_notifier: Notifier,
) -> None:
    registry.register(fake_notifier)
    assert registry.get("fake") is fake_notifier


def test_register_duplicate_raises_RegistrationError(
    registry: ExplicitNotifierRegistry,
    fake_notifier_class: type,
) -> None:
    registry.register(fake_notifier_class())
    with pytest.raises(RegistrationError, match="duplicate channel_id: fake"):
        registry.register(fake_notifier_class())


def test_register_non_notifier_raises_RegistrationError(
    registry: ExplicitNotifierRegistry,
) -> None:
    class NotANotifier:
        """Missing all Notifier Protocol members."""

    with pytest.raises(RegistrationError, match="does not satisfy Notifier Protocol"):
        registry.register(NotANotifier())  # type: ignore[arg-type]


def test_get_unknown_raises_KeyError(
    registry: ExplicitNotifierRegistry,
) -> None:
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_has_returns_bool(
    registry: ExplicitNotifierRegistry,
    fake_notifier: Notifier,
) -> None:
    assert registry.has("fake") is False
    registry.register(fake_notifier)
    assert registry.has("fake") is True
    assert registry.has("other") is False


def test_all_preserves_registration_order(
    registry: ExplicitNotifierRegistry,
) -> None:
    notifiers = [_make_fake_notifier_class(ch)() for ch in ("alpha", "beta", "gamma")]
    for n in notifiers:
        registry.register(n)
    result = registry.all()
    assert result == notifiers
    assert [type(n).channel_id for n in result] == ["alpha", "beta", "gamma"]


def test_registered_notifier_send_works(
    registry: ExplicitNotifierRegistry,
    fake_notifier: Notifier,
    lot_dto: LotPublicDTO,
) -> None:
    """All methods of the fake notifier are callable after registration."""
    registry.register(fake_notifier)
    retrieved = registry.get("fake")

    # send() — the primary contract method
    send_result = retrieved.send(lot_dto, "user@x")
    assert send_result.ok is True
    assert send_result.retryable is False

    # test() — secondary contract method
    test_result = retrieved.test("user@x")
    assert test_result.ok is True
