"""Tests: SmtpCredentials.from_name — Layer 0 domain model invariants (bd ljp).

Layer: 1 (Domain — Pydantic value-object validation).
No network, no DB, no SMTP.

Invariants checked:
  - from_name defaults to None (optional field).
  - from_name round-trips correctly through model construction.
  - SmtpCredentials is frozen — from_name cannot be mutated after construction.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from fis_monitor.domain.models import SmtpCredentials


def _make_creds(**overrides) -> SmtpCredentials:
    defaults = {
        "smtp_user": "bot@example.com",
        "smtp_password": SecretStr("s3cr3t"),
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
    }
    defaults.update(overrides)
    return SmtpCredentials(**defaults)


class TestFromNameDefaultsToNone:
    def test_from_name_absent_defaults_to_none(self) -> None:
        """When from_name is not supplied, it must default to None."""
        creds = _make_creds()
        assert creds.from_name is None

    def test_from_name_explicit_none(self) -> None:
        """Explicit from_name=None is accepted and stored as None."""
        creds = _make_creds(from_name=None)
        assert creds.from_name is None


class TestFromNameRoundTrips:
    def test_from_name_stored_and_readable(self) -> None:
        """from_name is persisted in the model and readable afterwards."""
        creds = _make_creds(from_name="Монитор гектара")
        assert creds.from_name == "Монитор гектара"

    def test_from_name_empty_string_is_stored(self) -> None:
        """Empty string is a valid from_name value (falsy but not None)."""
        creds = _make_creds(from_name="")
        assert creds.from_name == ""

    def test_from_name_unicode(self) -> None:
        """from_name accepts arbitrary Unicode (Cyrillic, emoji not expected but valid)."""
        creds = _make_creds(from_name="Бот-уведомитель")
        assert creds.from_name == "Бот-уведомитель"


class TestFromNameFrozen:
    def test_from_name_immutable(self) -> None:
        """SmtpCredentials is frozen — mutating from_name must raise."""
        creds = _make_creds(from_name="Original")
        with pytest.raises((ValidationError, TypeError)):
            creds.from_name = "Changed"  # type: ignore[misc]
