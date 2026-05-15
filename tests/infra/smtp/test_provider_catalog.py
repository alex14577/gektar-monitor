"""Unit tests for StaticSmtpProviderCatalog (Layer 2 — infra/smtp).

Coverage (per ADR-038 §Тесты по слоям Layer 2):
  - Known domains return correct ProviderSuggestion (parametrize all catalog entries).
  - Unknown domain returns None.
  - Case-insensitive domain lookup (USER@Yandex.RU → same as user@yandex.ru).
  - Malformed / missing-@ inputs return None without raising.
  - CONTRACT: every catalog host passes DefaultSmtpHostPolicy._reject_pre_resolve
    (critical security invariant — auto-suggested hosts must not be on blocklist).

Invariants tested:
  - lookup() never raises on any string input.
  - Case normalisation is applied before dict key lookup.
  - All catalog smtp_host values survive pre-resolve policy check.
"""

from __future__ import annotations

import pytest

from fis_monitor.infra.smtp.host_policy import _reject_pre_resolve
from fis_monitor.infra.smtp.provider_catalog import _CATALOG, StaticSmtpProviderCatalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def catalog() -> StaticSmtpProviderCatalog:
    return StaticSmtpProviderCatalog()


# ---------------------------------------------------------------------------
# Known domains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected_host", "expected_port", "expected_starttls"),
    [
        ("user@yandex.ru",    "smtp.yandex.ru",      465, False),
        ("user@yandex.com",   "smtp.yandex.ru",      465, False),
        ("user@ya.ru",        "smtp.yandex.ru",      465, False),
        ("user@mail.ru",      "smtp.mail.ru",        465, False),
        ("user@list.ru",      "smtp.mail.ru",        465, False),
        ("user@inbox.ru",     "smtp.mail.ru",        465, False),
        ("user@bk.ru",        "smtp.mail.ru",        465, False),
        ("user@rambler.ru",   "smtp.rambler.ru",     465, False),
        ("user@lenta.ru",     "smtp.rambler.ru",     465, False),
        ("user@autorambler.ru", "smtp.rambler.ru",   465, False),
        ("user@gmail.com",    "smtp.gmail.com",      587, True),
        ("user@googlemail.com", "smtp.gmail.com",    587, True),
        ("user@outlook.com",  "smtp.office365.com",  587, True),
        ("user@hotmail.com",  "smtp.office365.com",  587, True),
        ("user@live.com",     "smtp.office365.com",  587, True),
        ("user@msn.com",      "smtp.office365.com",  587, True),
        ("user@icloud.com",   "smtp.mail.me.com",    587, True),
        ("user@me.com",       "smtp.mail.me.com",    587, True),
        ("user@mac.com",      "smtp.mail.me.com",    587, True),
        ("user@yahoo.com",    "smtp.mail.yahoo.com", 465, False),
        ("user@yahoo.ru",     "smtp.mail.yahoo.com", 465, False),
        ("user@ymail.com",    "smtp.mail.yahoo.com", 465, False),
    ],
)
def test_known_domains_return_suggestion(
    catalog: StaticSmtpProviderCatalog,
    email: str,
    expected_host: str,
    expected_port: int,
    expected_starttls: bool,
) -> None:
    result = catalog.lookup(email)
    assert result is not None, f"Expected suggestion for {email!r}, got None"
    assert result.smtp_host == expected_host
    assert result.smtp_port == expected_port
    assert result.use_starttls == expected_starttls


# ---------------------------------------------------------------------------
# Unknown domain
# ---------------------------------------------------------------------------


def test_unknown_domain_returns_none(catalog: StaticSmtpProviderCatalog) -> None:
    assert catalog.lookup("user@unknown.example.com") is None
    assert catalog.lookup("user@mycompany.ru") is None
    assert catalog.lookup("user@corp.internal") is None


# ---------------------------------------------------------------------------
# Case-insensitive lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "USER@YANDEX.RU",
        "User@Yandex.Ru",
        "user@YANDEX.RU",
        "FIRST.LAST@Gmail.COM",
        "Me@OUTLOOK.COM",
    ],
)
def test_case_insensitive_domain_lookup(
    catalog: StaticSmtpProviderCatalog, email: str
) -> None:
    result = catalog.lookup(email)
    assert result is not None, f"Case-insensitive lookup failed for {email!r}"


def test_case_insensitive_yandex_exact_host(
    catalog: StaticSmtpProviderCatalog,
) -> None:
    result = catalog.lookup("USER@Yandex.RU")
    assert result is not None
    assert result.smtp_host == "smtp.yandex.ru"


# ---------------------------------------------------------------------------
# Malformed / invalid inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "notanemail",
        "@nodomain",
        "user@",
        "   ",
        "\t\n",
        "@",
    ],
)
def test_invalid_email_returns_none(
    catalog: StaticSmtpProviderCatalog, bad_input: str
) -> None:
    # Must not raise — always return None for unparseable inputs.
    result = catalog.lookup(bad_input)
    assert result is None, f"Expected None for {bad_input!r}, got {result!r}"


# ---------------------------------------------------------------------------
# CONTRACT: all catalog hosts pass DefaultSmtpHostPolicy._reject_pre_resolve
# ---------------------------------------------------------------------------


def test_all_catalog_hosts_pass_pre_resolve_policy() -> None:
    """Security invariant: no catalog entry maps to a blocked/internal host.

    If this test fails, a catalog entry would bypass the host-policy blocklist
    (ADR-015) in the suggestion path. The test calls _reject_pre_resolve
    directly — the same check applied by DefaultSmtpHostPolicy.resolve_and_check
    before any DNS lookup.
    """
    failures: list[str] = []
    for domain, suggestion in _CATALOG.items():
        try:
            _reject_pre_resolve(suggestion.smtp_host)
        except Exception as exc:
            failures.append(f"{domain} -> {suggestion.smtp_host!r}: {exc}")

    assert not failures, (
        "Catalog entries failed pre-resolve policy check:\n"
        + "\n".join(failures)
    )
