"""SettingsService — SMTP credentials save with DNS-outside-tx invariant.

Implements the three-phase flow from docs/architecture/03-protocols.md §3.3:
  1. Pydantic format-validation — already done at SmtpCredentials construction.
  2. DNS resolve + policy check (SmtpHostPolicy.resolve_and_check) — outside tx.
  3. Persist via SmtpCredentialsRepository.save() — short BEGIN IMMEDIATE tx.

Holding the writer-lock while DNS resolves is explicitly prohibited (ADR-015,
docs/architecture/03-protocols.md §3.3).  This service enforces that invariant
by calling resolve_and_check BEFORE delegating to the repository.

No HMAC on MVP per ADR-018 known-limitation R3-M10 (single-user, ACL on
%LOCALAPPDATA% is the trust boundary).
"""

from __future__ import annotations

from fis_monitor.domain.interfaces import SmtpCredentialsRepository, SmtpHostPolicy
from fis_monitor.domain.models import SmtpCredentials


class SettingsService:
    """Application service: persist validated SMTP credentials.

    Responsibilities (SRP):
    - Enforce that DNS resolution happens *outside* any database transaction.
    - Delegate format-validation to the Pydantic model (already immutable on
      entry) and policy-validation to ``SmtpHostPolicy``.
    - Delegate persistence to ``SmtpCredentialsRepository``.

    This class intentionally has no knowledge of SQLite transactions — that is
    the repository's concern.  The service only controls *ordering* (resolve
    before save).

    Args:
        smtp_creds_repo: Repository for SMTP credentials persistence.
        host_policy:     DNS-resolve + security policy checker.
    """

    def __init__(
        self,
        smtp_creds_repo: SmtpCredentialsRepository,
        host_policy: SmtpHostPolicy,
    ) -> None:
        self._smtp_creds_repo = smtp_creds_repo
        self._host_policy = host_policy

    def set_smtp_credentials(self, creds: SmtpCredentials) -> None:
        """Validate, DNS-check, then persist SMTP credentials.

        Phase 1 — Format validation: ``creds`` is a frozen Pydantic model;
            validation already occurred at construction time.  We perform an
            additional guard for empty host/port to surface a clear ValueError
            instead of a cryptic DNS error.

        Phase 2 — Policy check (DNS, up to 5 s): ``host_policy.resolve_and_check``
            is called OUTSIDE any database transaction.  If ``SmtpHostPolicyError``
            is raised it propagates to the caller (UI will display the message).
            The resolved ``ResolvedSmtpEndpoint`` is intentionally discarded —
            it is an infra-runtime DTO; the repository stores host+port as
            entered by the user, and resolve_and_check is repeated on every send.

        Phase 3 — Persistence: ``smtp_creds_repo.save(creds)`` runs its own
            short ``BEGIN IMMEDIATE`` transaction.

        Raises:
            ValueError: ``creds.smtp_host`` is empty or ``creds.smtp_port`` is 0.
            SmtpHostPolicyError: DNS resolution or policy check failed.
        """
        # Phase 1 — guard against empty host/port (Pydantic allows non-empty
        # strings but does not check semantic emptiness for smtp_host).
        if not creds.smtp_host.strip():
            raise ValueError("smtp_host must not be empty")
        if not (1 <= creds.smtp_port <= 65535):
            raise ValueError(f"smtp_port {creds.smtp_port!r} is out of valid range 1-65535")

        # Phase 2 — DNS resolve + policy check, outside any transaction.
        # SmtpHostPolicyError propagates to the caller as-is.
        self._host_policy.resolve_and_check(creds.smtp_host, creds.smtp_port)

        # Phase 3 — short BEGIN IMMEDIATE tx inside repository.
        self._smtp_creds_repo.save(creds)
