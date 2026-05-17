"""SMTP email notifier — SmtpEmailNotifier + EmailNotifierConfig.

Implements the ``Notifier`` Protocol (domain/interfaces.py §Layer 3) for the
``email`` channel.

**Why manual STARTTLS / manual implicit-TLS (ADR-021 + amendment):**
``smtplib.SMTP.starttls()`` passes ``self._host`` as ``server_hostname`` for
SNI / TLS-cert verification.  When we connect by pinned IP (closing TOCTOU per
ADR-015 R3-C4, ``SMTP(host=endpoint.ip)``), ``self._host = endpoint.ip``.
TLS cert verification then runs ``ip_literal`` against the server certificate
whose CN/SANs contain the DNS hostname → ``ssl.SSLCertVerificationError``.

STARTTLS fix (port 587): skip ``smtp.starttls()`` entirely, send the
``STARTTLS`` command via ``smtp.docmd("STARTTLS")``, then call
``ctx.wrap_socket(smtp.sock, server_hostname=endpoint.original_host)``
directly.  ``smtp.file = None`` invalidates smtplib's cached file-wrapper.

Implicit-TLS fix (port 465): ``smtplib.SMTP_SSL(host=endpoint.ip)`` sets
``self._host = endpoint.ip``, so SNI is wrong for the same reason.  Instead:
open a raw TCP socket to the pinned IP, wrap it with the correct SNI via
``ctx.wrap_socket(sock, server_hostname=endpoint.original_host)``, then pass
the wrapped socket to ``smtplib.SMTP(sock=wrapped_sock)``.  No STARTTLS
command needed — TLS is established before the SMTP banner.

TLS mode is derived from the port: port 465 → implicit TLS; all other ports →
STARTTLS.  This matches the semantics in ``infra/smtp/provider_catalog.py``
without requiring a separate ``use_starttls`` field in ``SmtpCredentials``.

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
import socket
import ssl
from email.headerregistry import Address
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
    ResolvedSmtpEndpoint,
    SmtpCredentials,
)
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.infra.smtp.host_policy import SmtpHostPolicy

logger = logging.getLogger(__name__)

_IMPLICIT_TLS_PORT = 465


class _StarttlsRefused(Exception):
    """Internal sentinel: STARTTLS command rejected by server (non-220 response)."""

    def __init__(self, code: int) -> None:
        self.code = code

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
    smtp_host: str | None = None
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
        url_builder: TorgiUrlBuilder,
        connect_timeout: float = 10.0,
    ) -> None:
        self._smtp_creds_repo = smtp_creds_repo
        # config_source/clock currently unused at instance level; reserved for
        # future stamping per ADR-019 R4-M6 retry-counter audit.
        self._config_source = config_source
        self._clock = clock
        self._host_policy = host_policy
        self._url_builder = url_builder
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

    def send_session_expired(self, recipient: str) -> NotifyResult:
        """Send a session-expired notification to *recipient*.

        Subject: «Сессия FIS истекла, мониторинг приостановлен».
        Message-ID uses a dedicated ``session_expired`` slot (no lot context).
        Never raises for expected failures — returns :class:`~NotifyResult`.
        """
        creds = self._load_creds()
        if creds is None:
            return NotifyResult(
                ok=False, detail="no_smtp_credentials", retryable=False
            )

        msg = self._build_session_expired_message(recipient=recipient, creds=creds)
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

    @staticmethod
    def _from_address(creds: SmtpCredentials) -> str | Address:
        """Build the RFC 5322 ``From:`` value.

        When ``creds.from_name`` is set, returns an :class:`email.headerregistry.Address`
        with a display name so that MUAs render ``"Display Name" <user@host>``.
        When ``from_name`` is ``None`` or empty, returns the bare ``smtp_user`` string.

        The returned value is assigned directly to ``msg["From"]`` — both ``str``
        and ``Address`` are accepted by :class:`email.message.EmailMessage`.
        """
        if not creds.from_name:
            return creds.smtp_user
        # email.headerregistry.Address handles RFC 5322 quoting of the display name.
        return Address(display_name=creds.from_name, addr_spec=creds.smtp_user)

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
        url = self._url_builder.lot_detail_url(lot_id=lot.id)
        msg.set_content(
            f"Лот #{lot.id} изменил статус.\n\n"
            f"Регион: {lot.region}\n"
            f"Статус: {lot.status}\n"
            f"Ссылка: {url}\n"
        )
        return msg

    def _build_session_expired_message(
        self,
        *,
        recipient: str,
        creds: SmtpCredentials,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._from_address(creds)
        msg["To"] = recipient
        msg["Subject"] = "Сессия FIS истекла, мониторинг приостановлен"
        # Message-ID: use lot_id=−1 as a sentinel for session-expired messages
        # so deduplication logic in MTA treats these as distinct from lot notifications.
        rh = self._recipient_hash(recipient)
        msg["Message-ID"] = f"<session_expired.{self.channel_id}.{rh}@fis-monitor.local>"
        msg.set_content(
            "Сессия ФИС истекла. Мониторинг лотов приостановлен до повторного входа.\n\n"
            "Войдите заново по ссылке: http://127.0.0.1:8080/\n"
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
        """Connect, authenticate, and send via SMTP.

        Supports two TLS modes derived from the port number:
        - Port 465 → implicit TLS (SMTPS): TLS handshake before SMTP banner.
        - Any other port → STARTTLS: plain TCP connect, then upgrade via manual
          STARTTLS command (ADR-021).

        Both paths use connect-by-IP + explicit SNI to avoid ADR-021 hostname bug.

        Error mapping — PII-free codes; recipient/password NEVER in detail:

        * ``ssl.SSLError`` → retryable=True, detail="tls_<ExcType>"
          (precedes OSError in handler order — ssl.SSLError inherits OSError)
        * ``SMTPServerDisconnected`` → retryable=True, detail="server_disconnected"
          (precedes OSError in handler order — SMTPServerDisconnected inherits OSError)
        * ``TimeoutError`` / ``ConnectionError`` / ``OSError``
          → retryable=True, detail="connect_error_<ExcType>"
        * ``SMTPAuthenticationError`` → retryable=False, detail="auth_failed"
        * ``SMTPRecipientsRefused`` → retryable=False, detail="recipient_refused"
        * ``SMTPResponseException`` 4xx → retryable=True, detail="smtp_<code>"
        * ``SMTPResponseException`` 5xx → retryable=False, detail="smtp_<code>"
        * ``SmtpHostPolicyError`` → raised (programming/config error)

        Connect and send phases share a single try block so that
        ``SMTPServerDisconnected`` from the implicit-TLS banner/ehlo sequence
        is handled identically to the send-phase disconnect.
        """
        # SmtpHostPolicyError is intentionally NOT caught — let it propagate.
        endpoint = self._host_policy.resolve_and_check(
            creds.smtp_host, creds.smtp_port
        )

        implicit_tls = endpoint.port == _IMPLICIT_TLS_PORT

        smtp: smtplib.SMTP | None = None
        try:
            if implicit_tls:
                smtp = self._connect_implicit_tls(endpoint)
            else:
                smtp = self._connect_starttls(endpoint)

            smtp.login(creds.smtp_user, creds.smtp_password.get_secret_value())
            smtp.send_message(msg)

        except _StarttlsRefused as exc:
            return NotifyResult(
                ok=False,
                detail=f"starttls_refused_code_{exc.code}"[:500],
                retryable=True,
            )

        except smtplib.SMTPAuthenticationError:
            return NotifyResult(ok=False, detail=_DETAIL_AUTH_FAILED, retryable=False)

        except smtplib.SMTPRecipientsRefused:
            return NotifyResult(
                ok=False, detail=_DETAIL_RECIPIENT_REFUSED, retryable=False
            )

        except smtplib.SMTPServerDisconnected:
            # Must precede OSError: SMTPServerDisconnected inherits from OSError.
            return NotifyResult(
                ok=False, detail=_DETAIL_SERVER_DISCONNECTED, retryable=True
            )

        except smtplib.SMTPResponseException as exc:
            retryable = exc.smtp_code < 500
            detail = f"smtp_{exc.smtp_code}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=retryable)

        except ssl.SSLError as exc:
            # Must precede OSError: ssl.SSLError inherits from OSError.
            detail = f"tls_{type(exc).__name__}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=True)

        except (TimeoutError, ConnectionError, OSError) as exc:
            detail = f"connect_error_{type(exc).__name__}"[:500]
            return NotifyResult(ok=False, detail=detail, retryable=True)

        else:
            return NotifyResult(ok=True, detail=_DETAIL_SENT, retryable=False)

        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except (smtplib.SMTPException, OSError, ssl.SSLError):
                    logger.debug("smtp quit failed", exc_info=True)
                    smtp.close()

    def _connect_starttls(self, endpoint: ResolvedSmtpEndpoint) -> smtplib.SMTP:
        """Open a plain TCP connection then upgrade to TLS via manual STARTTLS.

        ADR-021: avoids smtplib SNI bug when connecting by pinned IP.
        Returns a ready-to-authenticate SMTP instance (post-EHLO, post-TLS).
        Raises _StarttlsRefused if the server rejects the STARTTLS command.
        Raises OSError/TimeoutError/ssl.SSLError on connect/TLS failure.
        """
        smtp = smtplib.SMTP(
            host=endpoint.ip,
            port=endpoint.port,
            timeout=self._connect_timeout,
        )
        smtp.ehlo(endpoint.original_host)

        code, _ = smtp.docmd("STARTTLS")
        if code != 220:
            smtp.close()
            raise _StarttlsRefused(code)

        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        smtp.sock = ctx.wrap_socket(
            smtp.sock, server_hostname=endpoint.original_host
        )
        smtp.file = None  # invalidate cached file-wrapper (ADR-021)
        smtp.ehlo(endpoint.original_host)
        return smtp

    def _connect_implicit_tls(self, endpoint: ResolvedSmtpEndpoint) -> smtplib.SMTP:
        """Open a TLS-wrapped TCP connection for implicit TLS (SMTPS, port 465).

        ADR-021 amendment: ``smtplib.SMTP_SSL(host=endpoint.ip)`` sets
        ``self._host = endpoint.ip`` → wrong SNI.  Instead: create a raw TCP
        socket to the pinned IP, wrap it with the correct SNI via
        ``ctx.wrap_socket(server_hostname=endpoint.original_host)``, then
        inject the wrapped socket directly into an ``smtplib.SMTP`` instance
        that was constructed without auto-connect (``host=''``).

        Returns a ready-to-authenticate SMTP instance (post-banner, post-EHLO).
        Raises OSError/TimeoutError/ssl.SSLError on connect/TLS failure.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        raw_sock = socket.create_connection(
            (endpoint.ip, endpoint.port),
            timeout=self._connect_timeout,
        )
        try:
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=endpoint.original_host)
        except BaseException:
            raw_sock.close()
            raise
        smtp = smtplib.SMTP(timeout=self._connect_timeout)
        smtp.sock = tls_sock
        smtp.getreply()  # read the 220 banner
        smtp.ehlo(endpoint.original_host)
        return smtp
