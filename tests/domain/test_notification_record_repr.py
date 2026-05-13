"""Tests for NotificationRecord.__repr__ — PII-safe representation."""

from __future__ import annotations

from tests.factories import make_notification


def test_repr_does_not_contain_recipient_address() -> None:
    """__repr__ must not leak the plaintext recipient email."""
    record = make_notification(recipient="alice@example.com")
    r = repr(record)
    assert "alice" not in r
    assert "example.com" not in r


def test_repr_contains_sha256_marker() -> None:
    """__repr__ must include the sha256 hash marker for auditing."""
    record = make_notification(recipient="alice@example.com")
    r = repr(record)
    assert "sha256" in r


def test_repr_contains_non_pii_fields() -> None:
    """__repr__ must include lot_id, channel, status, attempt_no."""
    record = make_notification(
        lot_id=42,
        channel="email",
        recipient="bob@corp.org",
        status="pending",
        attempt_no=2,
    )
    r = repr(record)
    assert "42" in r
    assert "email" in r
    assert "pending" in r
    assert "2" in r


def test_model_dump_still_contains_recipient() -> None:
    """model_dump() must still expose the full recipient (not affected by __repr__)."""
    record = make_notification(recipient="alice@example.com")
    data = record.model_dump()
    assert data["recipient"] == "alice@example.com"
