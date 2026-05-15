"""Static SMTP provider catalog — domain-to-endpoint mapping (ADR-038).

Single responsibility: map a known email domain to a ``ProviderSuggestion``
DTO so the UI can pre-fill host/port/TLS without the user knowing provider
SMTP internals.

Design decisions (ADR-038):
- Hardcoded dict — versioned in git, zero runtime I/O, deterministic.
- No MX/SRV/ISPDB lookups — no network dependency in a UX helper.
- All catalog hosts MUST pass ``DefaultSmtpHostPolicy._reject_pre_resolve``;
  the contract is asserted by ``test_provider_catalog.py``.
- Domain matching is case-insensitive; full ``@domain`` split is used.
- Aliases (yandex.com → yandex.ru, hotmail.com → outlook.com, etc.) are
  explicit keys mapping to the canonical ``ProviderSuggestion``.

Security note: catalog is UX-only. The save path always runs
``DefaultSmtpHostPolicy.resolve_and_check`` regardless of suggestion origin.
"""

from __future__ import annotations

from fis_monitor.domain.models import ProviderSuggestion

# ---------------------------------------------------------------------------
# Catalog entries (canonical + aliases)
# ---------------------------------------------------------------------------

_yandex = ProviderSuggestion(
    smtp_host="smtp.yandex.ru",
    smtp_port=465,
    use_starttls=False,
    app_password_url="https://yandex.ru/support/mail/mail-clients/others.html",
    provider_label="Yandex",
)

_mailru = ProviderSuggestion(
    smtp_host="smtp.mail.ru",
    smtp_port=465,
    use_starttls=False,
    app_password_url="https://help.mail.ru/mail/security/protection/external",
    provider_label="Mail.ru",
)

_rambler = ProviderSuggestion(
    smtp_host="smtp.rambler.ru",
    smtp_port=465,
    use_starttls=False,
    app_password_url=None,
    provider_label="Rambler",
)

_gmail = ProviderSuggestion(
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    use_starttls=True,
    app_password_url="https://support.google.com/accounts/answer/185833",
    provider_label="Gmail",
)

_outlook = ProviderSuggestion(
    smtp_host="smtp.office365.com",
    smtp_port=587,
    use_starttls=True,
    app_password_url=(
        "https://support.microsoft.com/account-billing/"
        "manage-app-passwords-for-two-step-verification-"
        "d6dc8c6d-4bf7-4851-ad95-6d07799387e9"
    ),
    provider_label="Outlook",
)

_icloud = ProviderSuggestion(
    smtp_host="smtp.mail.me.com",
    smtp_port=587,
    use_starttls=True,
    app_password_url="https://support.apple.com/HT204397",
    provider_label="iCloud",
)

_yahoo = ProviderSuggestion(
    smtp_host="smtp.mail.yahoo.com",
    smtp_port=465,
    use_starttls=False,
    app_password_url="https://help.yahoo.com/kb/SLN15241.html",
    provider_label="Yahoo",
)

#: Module-level catalog dict — domain (lowercase) → ProviderSuggestion.
#: Aliases map to the same suggestion instance (identity, not copy).
_CATALOG: dict[str, ProviderSuggestion] = {
    # Yandex — canonical + aliases
    "yandex.ru":  _yandex,
    "yandex.com": _yandex,
    "ya.ru":      _yandex,
    # Mail.ru — canonical + aliases
    "mail.ru":    _mailru,
    "list.ru":    _mailru,
    "inbox.ru":   _mailru,
    "bk.ru":      _mailru,
    # Rambler — canonical + aliases
    "rambler.ru":      _rambler,
    "lenta.ru":        _rambler,
    "autorambler.ru":  _rambler,
    # Gmail — canonical + alias
    "gmail.com":      _gmail,
    "googlemail.com": _gmail,
    # Outlook / Microsoft — canonical + aliases
    "outlook.com": _outlook,
    "hotmail.com": _outlook,
    "live.com":    _outlook,
    "msn.com":     _outlook,
    # iCloud — canonical + aliases
    "icloud.com": _icloud,
    "me.com":     _icloud,
    "mac.com":    _icloud,
    # Yahoo — canonical + aliases
    "yahoo.com": _yahoo,
    "yahoo.ru":  _yahoo,
    "ymail.com": _yahoo,
}


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class StaticSmtpProviderCatalog:
    """In-memory SMTP provider catalog backed by ``_CATALOG``.

    Satisfies the ``SmtpProviderCatalog`` Protocol (structural typing —
    no explicit inheritance needed).

    Thread-safe: ``_CATALOG`` is module-level read-only; ``lookup`` has no
    mutable state.
    """

    def lookup(self, email: str) -> ProviderSuggestion | None:
        """Return a ``ProviderSuggestion`` for *email*'s domain, or ``None``.

        Returns ``None`` (never raises) for:
        - Malformed inputs (no ``@``, empty string, whitespace-only).
        - Domains not in the catalog.

        Domain matching is case-insensitive: ``USER@Yandex.RU`` is treated
        the same as ``user@yandex.ru``.

        Args:
            email: Full email address to look up.

        Returns:
            ``ProviderSuggestion`` on match, ``None`` otherwise.
        """
        if not email or "@" not in email:
            return None
        _, _, domain_part = email.partition("@")
        domain = domain_part.strip().lower()
        if not domain:
            return None
        return _CATALOG.get(domain)
