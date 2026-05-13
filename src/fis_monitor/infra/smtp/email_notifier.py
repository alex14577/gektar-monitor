"""SMTP email notifier — SmtpEmailNotifier + EmailNotifierConfig.

Implements the ``Notifier`` Protocol (domain/interfaces.py §Layer 3) for the
``email`` channel.

**Why manual STARTTLS (ADR-021):**
``smtplib.SMTP.starttls()`` passes ``self._host`` as ``server_hostname`` for
SNI / TLS-cert verification.  When we connect by pinned IP (closing TOCTOU per
ADR-015 R3-C4, ``SMTP(host=endpoint.ip)``), ``self._host = endpoint.ip``.
TLS cert verification then runs ``ip_literal`` against the server certificate
whose CN/SANs contain the DNS hostname → ``ssl.SSLCertVerificationError``.

Fix: skip ``smtp.starttls()`` entirely, send the ``STARTTLS`` command via
``smtp.docmd("STARTTLS")``, then call ``ctx.wrap_socket(smtp.sock,
server_hostname=endpoint.original_host)`` directly.  ``smtp.file = None``
invalidates smtplib's cached file-wrapper so subsequent reads/writes go
through the upgraded TLS socket.

**Message-ID determinism (ADR-019 R4-C5):**
``<{lot_id}.{channel_id}.{sha256(recipient)[:16]}@fis-monitor.local>`` —
MTA deduplication mitigation for the at-least-once crash window between
SMTP-ACK and ``mark_sent`` COMMIT.  Recipient is hashed to avoid PII leakage
into MTA Received-headers / bounce chains.
"""

from __future__ import annotations

import hashlib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import ClassVar

from pydantic import EmailStr, Field

from fis_monitor.domain.interfaces import (
    Clock,
    ConfigSource,
    SmtpCredentialsRepository,
)
from fis_monitor.domain.models import (
    LotPublicDTO,
    NotifierConfig,
    NotifyResult,
    SmtpCredentials,
)
from fis_monitor.infra.smtp.host_policy import SmtpHostPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EmailNotifierConfig — plugin config schema (high cohesion: lives next to notifier)
# ---------------------------------------------------------------------------


class EmailNotifierConfig(NotifierConfig):
    """Per-channel configuration for :class:`SmtpEmailNotifier`.

    Persisted in ``config.json`` under ``notifiers.email``.

    Fields:
        enabled: Whether the email channel is active.
        use_default_smtp: Use credentials stored in ``state.db`` (ADR-020
            SMTP SSOT); if ``False`` the per-config ``smtp_host``/port fields
            override.
        smtp_host: SMTP host (used when ``use_default_smtp=False``).
        smtp_port: SMTP port (used when ``use_default_smtp=False``).
        recipients: List of destination email addresses.

    Note: ``from_address`` override is intentionally omitted (YAGNI).
    Sender is always ``smtp_user`` from credentials.  Add when UI supports it.
    """

    enabled: bool = False
    use_default_smtp: bool = True
    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = Field(587, ge=1, le=65535)
    recipients: list[EmailStr] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SmtpEmailNotifier
# ---------------------------------------------------------------------------

# Detail codes — NEVER include PII (recipient email, password).
# Static codes below; dynamic codes derived from exception type names / SMTP
# response codes (e.g. "tls_SSLCertVerificationError", "smtp_421") — see _deliver().
_DETAIL_SENT = "sent"
_DETAIL_AUTH_FAILED = "auth_failed"
_DETAIL_RECIPIENT_REFUSED = "recipient_refused"
_DETAIL_SERVER_DISCONNECTED = "server_disconnected"


class SmtpEmailNotifier:
    """Email notification channel via SMTP with manual STARTTLS (ADR-021).

    Implements :class:`fis_monitor.domain.interfaces.Notifier` Protocol.

    Design:
    * **Synchronous** — callers (NotifierDispatcher) run in a dedicated thread.
    * **Thread-safe** — no mutable instance state beyond injected collaborators.
    * **DI via constructor** — all external dependencies injected.
    * **Result-pattern** — never raises for expected network / auth failures;
      returns :class:`~fis_monitor.domain.models.NotifyResult`.
    * :exc:`fis_monitor.domain.errors.SmtpHostPolicyError` from
      ``host_policy.resolve_and_check()`` is intentionally NOT caught here —
      it is a programming / config error and must surface to the operator.
    """

    # --- Notifier Protocol ClassVars ---
    channel_id: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Email"
    description: ClassVar[str] = "Send email notifications via SMTP"
    config_schema: ClassVar[type[NotifierConfig]] = EmailNotifierConfig
    recipient_label: ClassVar[str] = "Email address"
    recipient_placeholder: ClassVar[str] = "user@example.com"

    def __init__(
        self,
        *,
        smtp_creds_repo: SmtpCredentialsRepository,
        config_source: ConfigSource,
        clock: Clock,
        host_policy: SmtpHostPolicy,
        connect_timeout: float = 10.0,
    ) -> None:
        self._smtp_creds_repo = smtp_creds_repo
        # config_source/clock currently unused at instance level; reserved for
        # future stamping per ADR-019 R4-M6 retry-counter audit.
        self._config_source = config_source
        self._clock = clock
        self._host_policy = host_policy
        self._connect_timeout = connect_timeout

    # ------------------------------------------------------------------
    # Public API — Notifier Protocol
    # ------------------------------------------------------------------

    def send(self, lot: LotPublicDTO, recipient: str) -> NotifyResult:
        """Deliver a lot notification to *recipient* via SMTP.

        Never raises for expected failures — returns ``NotifyResult``.
        :exc:`~fis_monitor.domain.errors.SmtpHostPolicyError` (config bug)
        is propagated unchanged.
        """
        creds = self._load_creds()
        if creds is None:
            return NotifyResult(
                ok=False, detail="no_smtp_credentials", retryable=False
            )

        msg = self._build_lot_message(lot=lot, recipient=recipient, creds=creds)
        return self._deliver(msg=msg, recipient=recipient, creds=creds)

    def test(self, recipient: str) -> NotifyResult:
        """Send a test message to *recipient* (no lot context).

        Subject: ``FIS Monitor — test message``.
        Message-ID uses ``lot_id=0``.
        """
        creds = self._load_creds()
        if creds is None:
            return NotifyResult(
                ok=False, detail="no_smtp_credentials", retryable=False
            )

        msg = self._build_test_message(recipient=recipient, creds=creds)
        return self._deliver(msg=msg, recipient=recipient, creds=creds)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_creds(self) -> SmtpCredentials | None:
        return self._smtp_creds_repo.load()

    @staticmethod
    def _recipient_hash(recipient: str) -> str:
        """SHA-256(recipient)[:16] hex — used in Message-ID and logs (no PII)."""
        return hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:16]

    def _make_message_id(self, lot_id: int, recipient: str) -> str:
        """Deterministic Message-ID per ADR-019 R4-C5.

        ``<{lot_id}.{channel_id}.{sha256(recipient)[:16]}@fis-monitor.local>``
        """
        rh = self._recipient_hash(recipient)
        return f"<{lot_id}.{self.channel_id}.{rh}@fis-monitor.local>"

    def _from_address(self, creds: SmtpCredentials) -> str:
        return creds.smtp_user

    def _build_lot_message(
        self,
        *,
        lot: LotPublicDTO,
        recipient: str,
        creds: SmtpCredentials,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._from_address(creds)
        msg["To"] = recipient
        msg["Subject"] = f"FIS Monitor — лот #{lot.id} обновлён"
        msg["Message-ID"] = self._make_message_id(lot.id, recipient)
        msg.set_content(
            f"Лот #{lot.id} изменил статус.\n\n"
            f"Регион: {lot.region}\n"
            f"Статус: {lot.status}\n"
        )
        return msg

    def _build_test_message(
        self,
        *,
        recipient: str,
        creds: SmtpCredentials,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._from_address(creds)
        msg["To"] = recipient
        msg["Subject"] = "FIS Monitor — test message"
        msg["Message-ID"] = self._make_message_id(0, recipient)
        msg.set_content(
            "Если вы видите это письмо, SMTP настроен правильно.\n"
        )
        return msg

    def _deliver(
        self,
        *,
        msg: EmailMessage,
        recipient: str,
        creds: SmtpCredentials,
    ) -> NotifyResult:
        """Perform manual STARTTLS connect + login + send.

        Error mapping — PII-free dynamic codes derived from exception type names
        and SMTP response codes; recipient/password are NEVER in detail:

        * ``socket.timeout`` / ``ConnectionError`` / ``ssl.SSLError``
          → retryable=True, detail="network_<ExcType>" or "tls_<ExcType>"
        * ``SMTPAuthenticationError`` → retryable=False, detail="auth_failed"
        * ``SMTPRecipientsRefused`` → retryable=False, detail="recipient_refused"
        * ``SMTPServerDisconnected`` → retryable=True, detail="server_disconnected"
        * ``SMTPResponseException`` 4xx → retryable=True, detail="smtp_<code>"
        * ``SMTPResponseException`` 5xx → retryable=False, detail="smtp_<code>"
        * ``SmtpHostPolicyError`` → raised (programming/config error)
        """
        # SmtpHostPolicyError is intentionally NOT caught — let it propagate.
        endpoint = self._host_policy.resolve_and_check(
            creds.smtp_host, creds.smtp_port
        )

        try:
            smtp = smtplib.SMTP(
                host=endpoint.ip,
                port=endpoint.port,
                timeout=self._connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            return NotifyResult(
                ok=False,
                detail=f"connect_error_{type(exc).__name__}"[:500],
                retryable=True,
            )

        try:
            smtp.ehlo(endpoint.original_host)

            # --- Manual STARTTLS (ADR-021) ---
            code, _ = smtp.docmd("STARTTLS")
            if code != 220:
                return NotifyResult(
                    ok=False,
                    detail=f"starttls_refused_code_{code}"[:500],
                    retryable=True,
                )

            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            smtp.sock = ctx.wrap_socket(
                smtp.sock, server_hostname=endpoint.original_host
            )
            smtp.file = None  # invalidate cached file-wrapper (ADR-021)
            smtp.ehlo(endpoint.original_host)  # second EHLO after TLS upgrade

            # --- Auth + deliver ---
            smtp.login(creds.smtp_user, creds.smtp_password.get_secret_value())
            smtp.send_message(msg)

        except smtplib.SMTPAuthenticationError:
            return NotifyResult(ok=False, detail=_DETAIL_AUTH_FAILED, retryable=False)

        except smtplib.SMTPRecipientsRefused:
            return NotifyResult(
                ok=False, detail=_DETAIL_RECIPIENT_REFUSED, retryable=False
            )

        except smtplib.SMTPServerDisconnected:
            return NotifyResult(
                ok=False, detail=_DETAIL_SERVER_DISCONNECTED, retryable=True
            )

        except smtplib.SMTPResponseException as exc:
            retryable = exc.smtp_code < 500
            detail = f"smtp_{exc.smtp_code}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=retryable)

        except ssl.SSLError as exc:
            detail = f"tls_{type(exc).__name__}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=True)

        except (TimeoutError, ConnectionError, OSError) as exc:
            detail = f"network_{type(exc).__name__}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=True)

        else:
            return NotifyResult(ok=True, detail=_DETAIL_SENT, retryable=False)

        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError, ssl.SSLError):
                logger.debug("smtp quit failed", exc_info=True)
                smtp.close()
