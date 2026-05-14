"""Tests: SmtpCredentials.smtp_password is never exposed via repr/str/model_dump.

Guarantees per ADR-017 and docs/architecture/02-layers-dip.md §SecretStr contract.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from fis_monitor.domain.models import SmtpCredentials

_PASSWORD = "hunter2"


@pytest.fixture()
def creds() -> SmtpCredentials:
    return SmtpCredentials(
        smtp_user="u",
        smtp_password=SecretStr(_PASSWORD),
        smtp_host="smtp.x",
        smtp_port=587,
    )


class TestReprMasksPassword:
    def test_repr_masks_password(self, creds: SmtpCredentials) -> None:
        """repr() must not expose the plaintext password."""
        result = repr(creds)
        assert _PASSWORD not in result

    def test_repr_contains_secretstr_marker(self, creds: SmtpCredentials) -> None:
        """repr() must contain the Pydantic SecretStr masking marker."""
        result = repr(creds)
        # Pydantic SecretStr repr is: SecretStr('**********')
        assert "**********" in result or "SecretStr(" in result


class TestStrMasksPassword:
    def test_str_masks_password(self, creds: SmtpCredentials) -> None:
        """str() must not expose the plaintext password."""
        result = str(creds)
        assert _PASSWORD not in result

    def test_str_contains_secretstr_marker(self, creds: SmtpCredentials) -> None:
        """str() must contain the Pydantic SecretStr masking marker."""
        result = str(creds)
        assert "**********" in result or "SecretStr(" in result


class TestGetSecretValueReturnsPlaintext:
    def test_get_secret_value_returns_plaintext(self, creds: SmtpCredentials) -> None:
        """get_secret_value() must return the actual password (needed for SMTP login)."""
        assert creds.smtp_password.get_secret_value() == _PASSWORD


class TestModelDumpMasksPassword:
    def test_model_dump_masks_password(self, creds: SmtpCredentials) -> None:
        """model_dump() must not expose the plaintext — critical for JSON logger path.

        Pydantic v2 serialises SecretStr as '**********' in model_dump().
        """
        dumped = creds.model_dump()
        smtp_password_value = dumped["smtp_password"]
        # Pydantic v2 returns the SecretStr object itself from model_dump() by default;
        # str() of it shows '**********'. Either the object is SecretStr (safe) or
        # it is already the masked string.
        if isinstance(smtp_password_value, SecretStr):
            # Object itself — safe; plaintext only via .get_secret_value()
            assert smtp_password_value.get_secret_value() == _PASSWORD
        else:
            # Serialised form must be the mask, never the plaintext
            assert _PASSWORD not in str(smtp_password_value)
            assert "**********" in str(smtp_password_value)

    def test_model_dump_json_masks_password(self, creds: SmtpCredentials) -> None:
        """model_dump_json() must not contain the plaintext password.

        This is the path used by JsonFormatter (plg.1) — logging serialisation.
        """
        json_str = creds.model_dump_json()
        assert _PASSWORD not in json_str
        assert "**********" in json_str
