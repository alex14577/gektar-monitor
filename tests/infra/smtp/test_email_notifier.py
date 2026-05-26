"""Integration tests for SmtpEmailNotifier.

Layer 3: real aiosmtpd server (STARTTLS + implicit TLS, self-signed cert).
Layer 2: pure-logic tests — Message-ID, PII absence, error mapping (no server).

Fakes: FakeSmtpCredentialsRepository, FakeSmtpHostPolicy, FakeConfigSource, FakeClock.
All tests assert NO PII (recipient email) appears in NotifyResult.detail.
"""

from __future__ import annotations

import queue
import re
import smtplib
import socket
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import (
    LotPublicDTO,
    ResolvedSmtpEndpoint,
    SmtpCredentials,
)
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier

_URL_BUILDER = TorgiUrlBuilder(base_url="https://example.test")

# ---------------------------------------------------------------------------
# Section 1: Constants
# ---------------------------------------------------------------------------

_RECIPIENT = "alice@example.com"
_RECIPIENT2 = "bob@example.org"
_IP = "1.2.3.4"
_HOST = "smtp.test.local"
_PORT = 587

_MESSAGE_ID_PATTERN = re.compile(r"^<\d+\.email\.[0-9a-f]{16}@fis-monitor\.local>$")


# ---------------------------------------------------------------------------
# Section 2: TLS cert generation (session scope)
# ---------------------------------------------------------------------------


def _generate_self_signed_cert() -> tuple[bytes, bytes]:
    """RSA-2048 + self-signed X.509 for smtp.test.local. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _HOST)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def _build_ssl_contexts(cert_pem: bytes, key_pem: bytes) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Build (server_ctx, client_ctx). client_ctx trusts the self-signed cert."""
    import tempfile

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with (
        tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf,
        tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf,
    ):
        cf.write(cert_pem)
        cf.flush()
        kf.write(key_pem)
        kf.flush()
        server_ctx.load_cert_chain(cf.name, kf.name)

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cadata=cert_pem.decode())
    return server_ctx, client_ctx


@pytest.fixture(scope="session")
def ssl_contexts() -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Session-scoped (server_ctx, client_ctx) for smtp.test.local."""
    cert_pem, key_pem = _generate_self_signed_cert()
    return _build_ssl_contexts(cert_pem, key_pem)


# ---------------------------------------------------------------------------
# Section 3: aiosmtpd controller helpers
# ---------------------------------------------------------------------------


class _CapturingHandler:
    """aiosmtpd handler that queues received messages."""

    def __init__(self) -> None:
        self.messages: queue.Queue[dict] = queue.Queue()

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.put({"rcpt_tos": list(envelope.rcpt_tos), "content": envelope.content})
        return "250 Message accepted for delivery"


def _accept_any_auth(mechanism: str, login: bytes, password: bytes) -> bool:
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_starttls_controller(
    handler: _CapturingHandler, server_ctx: ssl.SSLContext
) -> tuple[object, int]:
    """STARTTLS controller on 127.0.0.1, free port. Returns (controller, port)."""
    from aiosmtpd.controller import Controller

    port = _free_port()
    ctrl = Controller(
        handler, hostname="127.0.0.1", port=port, tls_context=server_ctx,
        auth_required=True,
        auth_require_tls=False,  # test-only; production servers enforce auth_require_tls=True
        auth_callback=_accept_any_auth,
    )
    ctrl.start()
    return ctrl, port


def _start_implicit_tls_controller(
    handler: _CapturingHandler, server_ctx: ssl.SSLContext
) -> tuple[object, int]:
    """Implicit-TLS (SMTPS) controller on 127.0.0.1, free port. Returns (controller, port)."""
    from aiosmtpd.controller import Controller

    port = _free_port()
    ctrl = Controller(
        handler, hostname="127.0.0.1", port=port, ssl_context=server_ctx,
        auth_required=True,
        auth_require_tls=False,  # test-only; production servers enforce auth_require_tls=True
        auth_callback=_accept_any_auth,
    )
    ctrl.start()
    return ctrl, port


class _SNICapturingController:
    """STARTTLS controller that captures the SNI server_name via ssl sni_callback."""

    def __init__(self, server_ctx: ssl.SSLContext) -> None:
        self._sni_queue: queue.Queue[str | None] = queue.Queue()
        server_ctx.sni_callback = self._on_sni  # type: ignore[attr-defined]
        self._ctrl = None

    def _on_sni(self, ssl_obj, server_name, original_ctx) -> None:
        self._sni_queue.put(server_name)

    def start(self, handler: _CapturingHandler, server_ctx: ssl.SSLContext) -> int:
        self._ctrl, port = _start_starttls_controller(handler, server_ctx)
        return port

    def stop(self) -> None:
        if self._ctrl:
            self._ctrl.stop()

    def get_sni(self, timeout: float = 3.0) -> str | None:
        try:
            return self._sni_queue.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Section 4 & 5: Fake collaborators
# ---------------------------------------------------------------------------


class FakeSmtpCredentialsRepository:
    """In-memory SmtpCredentialsRepository."""

    def __init__(self, creds: SmtpCredentials | None = None) -> None:
        self._creds = creds
        self._load_called = False
        self._save_called_with: SmtpCredentials | None = None

    def load(self) -> SmtpCredentials | None:
        self._load_called = True
        return self._creds

    def save(self, creds: SmtpCredentials) -> None:
        self._save_called_with = creds


class FakeConfigSource:
    def get(self):
        return {}

    def subscribe(self, cb):
        class _H:
            def __enter__(self): return self
            def __exit__(self, *_): pass
        return _H()


class FakeClock:
    def now(self):
        return datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class FakeSmtpHostPolicy:
    """Returns a pinned ResolvedSmtpEndpoint for any resolve_and_check call."""

    def __init__(
        self, *, ip: str = _IP, host: str = _HOST, port: int = _PORT,
        raise_exc: Exception | None = None,
    ) -> None:
        self._endpoint = ResolvedSmtpEndpoint(ip=ip, family=socket.AF_INET, port=port,
                                               original_host=host)
        self._raise_exc = raise_exc
        self.resolve_called_with: list[tuple[str, int]] = []

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        self.resolve_called_with.append((host, port))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._endpoint


# ---------------------------------------------------------------------------
# Section 6: Builder helpers
# ---------------------------------------------------------------------------


def _make_creds(**overrides) -> SmtpCredentials:
    defaults = dict(smtp_user="bot@example.com", smtp_password=SecretStr("s3cr3t"),
                    smtp_host=_HOST, smtp_port=_PORT, use_default=True)
    defaults.update(overrides)
    return SmtpCredentials(**defaults)


def _make_lot(**overrides) -> LotPublicDTO:
    defaults = dict(
        id=99, cadastral_no="27:23:0040000:0099", area_sqm=5000,
        region="Тестовый регион", municipality="Тестовый город",
        land_category="test-category", permitted_use="test-use", ogv="test-ogv",
        status="Свободен",
        date_create=datetime(2026, 5, 1, tzinfo=UTC),
        date_update=datetime(2026, 5, 13, tzinfo=UTC),
        lat=55.75, lon=37.61, has_boundaries=True, raw_json={}, parser_version=1,
        first_seen=datetime(2026, 5, 1, tzinfo=UTC),
        last_seen=datetime(2026, 5, 13, tzinfo=UTC),
        detail_fetched_at=datetime(2026, 5, 13, tzinfo=UTC),
        enrichment_status="done",
        last_seen_at=datetime(2026, 5, 13, tzinfo=UTC),
        is_active=True, inactive_reason=None, inactive_since=None,
        inactive_confirmed_at=None, age_seconds=60, tier="match", freshness="hot",
    )
    defaults.update(overrides)
    return LotPublicDTO(**defaults)


_NO_CREDS = object()


def _make_notifier(
    *, creds: SmtpCredentials | None | object = _NO_CREDS,
    host_policy: FakeSmtpHostPolicy | None = None,
    connect_timeout: float = 10.0,
) -> tuple[SmtpEmailNotifier, FakeSmtpCredentialsRepository, FakeSmtpHostPolicy]:
    resolved_creds: SmtpCredentials | None = (
        _make_creds() if creds is _NO_CREDS else creds  # type: ignore[assignment]
    )
    repo = FakeSmtpCredentialsRepository(creds=resolved_creds)
    hp = host_policy or FakeSmtpHostPolicy()
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo, config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=hp, url_builder=_URL_BUILDER, connect_timeout=connect_timeout,
    )
    return notifier, repo, hp


def _mock_starttls_smtp():
    """Return a MagicMock smtp instance pre-configured for STARTTLS happy path."""
    m = MagicMock()
    m.docmd.return_value = (220, b"Go ahead")
    m.sock = MagicMock()
    return m


def _send_and_capture_message(creds: SmtpCredentials) -> EmailMessage:
    lot = _make_lot(id=1)
    repo = FakeSmtpCredentialsRepository(creds=creds)
    hp = FakeSmtpHostPolicy()
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo, config_source=FakeConfigSource(),
        clock=FakeClock(), host_policy=hp, url_builder=_URL_BUILDER,
    )
    m = _mock_starttls_smtp()
    captured: list[EmailMessage] = []
    m.send_message.side_effect = captured.append
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        notifier.send(lot, _RECIPIENT)
    assert captured
    return captured[0]


# ---------------------------------------------------------------------------
# Section 7: STARTTLS + implicit TLS happy paths (real server)
# ---------------------------------------------------------------------------


def test_happy_path_starttls_real_server(ssl_contexts):
    """T1 — Real STARTTLS server: send completes ok=True; message arrives; ADR-021 SNI ok."""
    server_ctx, client_ctx = ssl_contexts
    handler = _CapturingHandler()
    ctrl, port = _start_starttls_controller(handler, server_ctx)
    try:
        creds = _make_creds(smtp_host=_HOST, smtp_port=port)
        hp = FakeSmtpHostPolicy(ip="127.0.0.1", host=_HOST, port=port)
        notifier = SmtpEmailNotifier(
            smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
            config_source=FakeConfigSource(), clock=FakeClock(), host_policy=hp,
            url_builder=_URL_BUILDER, connect_timeout=5.0,
        )
        with patch("ssl.create_default_context", return_value=client_ctx):
            result = notifier.send(_make_lot(id=1), _RECIPIENT)

        assert result.ok is True, f"Expected ok=True, got detail={result.detail!r}"
        assert result.detail == "sent"
        msg_data = handler.messages.get(timeout=3)
        assert _RECIPIENT in msg_data["rcpt_tos"]
    finally:
        ctrl.stop()


def test_happy_path_implicit_tls_real_server(ssl_contexts):
    """T25 — Real implicit-TLS server: _connect_implicit_tls succeeds; ADR-021 SNI ok."""
    server_ctx, client_ctx = ssl_contexts
    handler = _CapturingHandler()
    ctrl, port = _start_implicit_tls_controller(handler, server_ctx)
    try:
        notifier, _, _ = _make_notifier()  # used only for _connect_implicit_tls
        endpoint = ResolvedSmtpEndpoint(
            ip="127.0.0.1", family=socket.AF_INET, port=port, original_host=_HOST,
        )
        with patch("ssl.create_default_context", return_value=client_ctx):
            smtp_conn = notifier._connect_implicit_tls(endpoint)

        assert smtp_conn is not None
        assert smtp_conn.sock is not None
        try:
            smtp_conn.quit()
        except Exception:
            smtp_conn.close()
    finally:
        ctrl.stop()


# ---------------------------------------------------------------------------
# Section 8: AUTH failure path (real server)
# ---------------------------------------------------------------------------


def test_auth_failure_real_server(ssl_contexts):
    """T2 (real server) — AUTH failure: result has no PII; TLS succeeded."""
    server_ctx, client_ctx = ssl_contexts
    handler = _CapturingHandler()
    # Build controller WITHOUT auth_callback → server doesn't advertise AUTH
    from aiosmtpd.controller import Controller
    port = _free_port()
    ctrl = Controller(handler, hostname="127.0.0.1", port=port, tls_context=server_ctx)
    ctrl.start()
    try:
        creds = _make_creds(smtp_host=_HOST, smtp_port=port)
        hp = FakeSmtpHostPolicy(ip="127.0.0.1", host=_HOST, port=port)
        notifier = SmtpEmailNotifier(
            smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
            config_source=FakeConfigSource(), clock=FakeClock(), host_policy=hp,
            url_builder=_URL_BUILDER, connect_timeout=5.0,
        )
        with patch("ssl.create_default_context", return_value=client_ctx):
            result = notifier.send(_make_lot(), _RECIPIENT)

        assert result.ok is False
        assert result.detail == "auth_failed"
        assert _RECIPIENT not in result.detail
        # TLS must have passed — no tls_ error
        assert not result.detail.startswith("tls_")
    finally:
        ctrl.stop()


# ---------------------------------------------------------------------------
# Section 9: Recipient refused / 5xx error codes (mock)
# ---------------------------------------------------------------------------


def test_smtp_recipients_refused():
    """T8 — SMTPRecipientsRefused → retryable=False, detail=recipient_refused."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.send_message.side_effect = smtplib.SMTPRecipientsRefused({_RECIPIENT: (550, b"Unknown")})
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is False
    assert result.detail == "recipient_refused"
    assert _RECIPIENT not in result.detail


@pytest.mark.parametrize(
    "code,expected_retryable", [(421, True), (450, True), (550, False), (554, False)]
)
def test_smtp_response_exception_retryable(code, expected_retryable):
    """T16 — SMTPResponseException retryable based on 4xx/5xx code."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.send_message.side_effect = smtplib.SMTPResponseException(code, b"Error")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is expected_retryable
    assert result.detail == f"smtp_{code}"
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# Section 10: Connect-by-IP + SNI verification (ADR-021 critical invariant)
# ---------------------------------------------------------------------------


def test_connect_by_ip_sni_is_hostname(ssl_contexts):
    """T10 — ADR-021: notifier connects to 127.0.0.1 but SNI == smtp.test.local.

    Proof 1 (client-side): result.detail is not a tls_ error — cert verified ok.
    Proof 2 (server-side): sni_callback reports server_name == smtp.test.local.
    If SNI were set to the IP literal "127.0.0.1", cert validation would fail.
    """
    server_ctx, client_ctx = ssl_contexts
    handler = _CapturingHandler()
    sni_ctrl = _SNICapturingController(server_ctx)
    port = sni_ctrl.start(handler, server_ctx)
    try:
        creds = _make_creds(smtp_host=_HOST, smtp_port=port)
        hp = FakeSmtpHostPolicy(ip="127.0.0.1", host=_HOST, port=port)
        notifier = SmtpEmailNotifier(
            smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
            config_source=FakeConfigSource(), clock=FakeClock(), host_policy=hp,
            url_builder=_URL_BUILDER, connect_timeout=5.0,
        )
        with patch("ssl.create_default_context", return_value=client_ctx):
            result = notifier.send(_make_lot(id=10), _RECIPIENT)

        # TLS must not have failed (any tls_ detail = ADR-021 regression)
        assert not result.detail.startswith("tls_"), (
            f"ADR-021 FAIL: TLS error detail={result.detail!r}. "
            "SNI was likely set to IP literal, causing cert verification failure."
        )

        # Server-side SNI: must be hostname, not IP
        received_sni = sni_ctrl.get_sni(timeout=3.0)
        assert received_sni == _HOST, (
            f"ADR-021: server received SNI={received_sni!r}, expected {_HOST!r}"
        )
        assert received_sni != "127.0.0.1", "ADR-021: SNI must not be IP literal"
    finally:
        sni_ctrl.stop()


# ---------------------------------------------------------------------------
# Section 11: No-credentials, retryable/non-retryable codes (no server)
# ---------------------------------------------------------------------------


def test_no_credentials():
    """T15 — repo.load() returns None → ok=False, retryable=False, no SMTP created."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier(creds=None)
    with patch("smtplib.SMTP") as mock_smtp_class:
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is False
    mock_smtp_class.assert_not_called()
    assert _RECIPIENT not in result.detail


def test_smtp_auth_error():
    """T6 — SMTPAuthenticationError → retryable=False, detail=auth_failed."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad creds")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is False
    assert result.detail == "auth_failed"
    assert _RECIPIENT not in result.detail
    m.quit.assert_called()


def test_starttls_refused():
    """T5 — docmd("STARTTLS") returns non-220 → ok=False, retryable=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = MagicMock()
    m.docmd.return_value = (500, b"Not supported")
    m.sock = MagicMock()
    with patch("smtplib.SMTP", return_value=m), patch("ssl.create_default_context"):
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("starttls")
    assert _RECIPIENT not in result.detail


def test_socket_timeout():
    """T7 — TimeoutError mid-send → ok=False, retryable=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.send_message.side_effect = TimeoutError("timed out")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert _RECIPIENT not in result.detail


def test_smtp_server_disconnected():
    """T13 — SMTPServerDisconnected → ok=False, retryable=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.send_message.side_effect = smtplib.SMTPServerDisconnected("Connection closed")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert result.detail == "server_disconnected"


# ---------------------------------------------------------------------------
# Section 12: Message-ID determinism + PII absence (no server)
# ---------------------------------------------------------------------------


def test_message_id_format():
    """T2 — Message-ID matches <lot_id.email.16hexchars@fis-monitor.local>."""
    lot = _make_lot(id=42)
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    captured: list[EmailMessage] = []
    m.send_message.side_effect = captured.append
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        notifier.send(lot, _RECIPIENT)
    assert captured
    mid = captured[0]["Message-ID"]
    assert _MESSAGE_ID_PATTERN.match(mid), f"Bad Message-ID: {mid!r}"
    assert mid.startswith("<42.email.")
    assert _RECIPIENT not in mid
    assert "alice" not in mid
    assert "example.com" not in mid


def test_same_recipient_same_message_id():
    """T3 — Same lot + recipient → identical Message-ID (determinism)."""
    lot = _make_lot(id=7)
    notifier, _, _ = _make_notifier()
    mid1 = notifier._make_message_id(lot.id, _RECIPIENT)
    mid2 = notifier._make_message_id(lot.id, _RECIPIENT)
    assert mid1 == mid2


def test_different_recipient_different_message_id():
    """T4 — Different recipient → different hash portion in Message-ID."""
    lot = _make_lot(id=7)
    notifier, _, _ = _make_notifier()
    mid1 = notifier._make_message_id(lot.id, _RECIPIENT)
    mid2 = notifier._make_message_id(lot.id, _RECIPIENT2)
    assert mid1 != mid2
    assert mid1.split(".")[2].split("@")[0] != mid2.split(".")[2].split("@")[0]


@pytest.mark.parametrize("exc", [
    smtplib.SMTPAuthenticationError(535, b"bad creds"),
    smtplib.SMTPRecipientsRefused({_RECIPIENT: (550, b"Unknown")}),
    smtplib.SMTPServerDisconnected("disconnected"),
    smtplib.SMTPResponseException(421, b"Busy"),
    smtplib.SMTPResponseException(550, b"Unknown"),
    TimeoutError("timed out"),
    ConnectionError("refused"),
])
def test_no_pii_in_detail(exc):
    """T10 — recipient email never appears in NotifyResult.detail."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        m.login.side_effect = exc
    else:
        m.send_message.side_effect = exc
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert _RECIPIENT not in result.detail, f"PII leak: {result.detail!r}"
    assert "alice" not in result.detail
    assert "example.com" not in result.detail


# ---------------------------------------------------------------------------
# Remaining tests (T9, T11, T12, T14, T17-T24) — mock-level, no server
# ---------------------------------------------------------------------------


def test_smtp_host_policy_error_propagates():
    """T9 — SmtpHostPolicyError from host_policy is NOT caught."""
    hp = FakeSmtpHostPolicy(raise_exc=SmtpHostPolicyError("smtp host 'evil.corp' rejected"))
    notifier, _, _ = _make_notifier(host_policy=hp)
    with pytest.raises(SmtpHostPolicyError):
        notifier.send(_make_lot(), _RECIPIENT)


def test_test_method_message_id_and_subject():
    """T11 — test() uses lot_id=0 in Message-ID; subject contains 'test'."""
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    captured: list[EmailMessage] = []
    m.send_message.side_effect = captured.append
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.test(_RECIPIENT)
    assert result.ok is True
    assert captured
    mid = captured[0]["Message-ID"]
    assert mid.startswith("<0.email."), f"Expected lot_id=0 in Message-ID: {mid!r}"
    assert "test" in captured[0]["Subject"].lower()


def test_ssl_cert_verification_error():
    """T12 — SSLCertVerificationError from wrap_socket → retryable=True, detail starts tls_."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError("mismatch")
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("tls_")
    assert _RECIPIENT not in result.detail


def test_all_fake_methods_called_in_happy_path():
    """T14 — All fake collaborator methods exercised in happy-path send."""
    creds = _make_creds(smtp_host="smtp.production.example.com", smtp_port=587)
    repo = FakeSmtpCredentialsRepository(creds=creds)
    hp = FakeSmtpHostPolicy(host="smtp.production.example.com")
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo, config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=hp, url_builder=_URL_BUILDER, connect_timeout=5.0,
    )
    m = _mock_starttls_smtp()
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(_make_lot(id=55), _RECIPIENT)
    assert result.ok is True
    assert repo._load_called is True
    assert len(hp.resolve_called_with) == 1
    assert hp.resolve_called_with[0] == ("smtp.production.example.com", 587)


def test_constructor_accepts_canon_signature():
    """T17 — SmtpEmailNotifier constructor signature matches build_container §4.2."""
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(),
        config_source=FakeConfigSource(), clock=FakeClock(), host_policy=FakeSmtpHostPolicy(),
        url_builder=_URL_BUILDER,
    )
    assert notifier.channel_id == "email"


def test_lot_message_body_contains_url():
    """T26 — _build_lot_message includes correct lot URL; plain-text only (no multipart)."""
    lot = _make_lot(id=42)
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    captured: list[EmailMessage] = []
    m.send_message.side_effect = captured.append
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        notifier.send(lot, _RECIPIENT)
    assert captured
    msg = captured[0]
    body = msg.get_body()
    assert body is not None
    content = body.get_content()
    expected_url = "https://example.test/cabinet/free-lot-view?id=42"
    assert f"Ссылка: {expected_url}" in content, (
        f"URL line not found in body: {content!r}"
    )
    assert "42" in content
    # Plain-text only — must not be multipart
    assert not msg.is_multipart(), "Message must remain plain-text, not multipart"
    # T26b — cadastral_no present → map line included (colons literal, not URL-encoded)
    cadastral_no = lot.cadastral_no
    assert cadastral_no, "fixture must have a non-empty cadastral_no"
    expected_map_line = f"Карта: https://ik5map.roscadastres.com/map.html?cn={cadastral_no}"
    assert expected_map_line in content, (
        f"Map line not found in body: {content!r}"
    )


def test_lot_message_body_no_map_line_when_no_cadastral_no():
    """T26c — cadastral_no='' → no 'Карта:' line in email body."""
    lot = _make_lot(id=5, cadastral_no="")
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    captured: list[EmailMessage] = []
    m.send_message.side_effect = captured.append
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        notifier.send(lot, _RECIPIENT)
    assert captured
    msg = captured[0]
    body = msg.get_body()
    assert body is not None
    content = body.get_content()
    assert "Карта:" not in content, (
        f"'Карта:' line must be absent when cadastral_no is empty, got: {content!r}"
    )


def test_quit_failure_calls_close():
    """T18 — smtp.quit() raises in finally → smtp.close() called; send still ok=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()
    m = _mock_starttls_smtp()
    m.quit.side_effect = smtplib.SMTPServerDisconnected("already gone")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)
    assert result.ok is True
    m.quit.assert_called()
    m.close.assert_called()


def test_from_header_without_display_name() -> None:
    """T19a — from_name=None → From: header is the bare smtp_user email."""
    msg = _send_and_capture_message(_make_creds(from_name=None))
    from_header = msg["From"]
    assert "bot@example.com" in from_header
    assert "<" not in from_header or from_header.strip() == "bot@example.com"


def test_from_header_with_display_name() -> None:
    """T19b — from_name set → From: is RFC 5322 'Display Name <email>'."""
    msg = _send_and_capture_message(_make_creds(from_name="Монитор гектара"))
    from_header = str(msg["From"])
    assert "bot@example.com" in from_header
    assert "<bot@example.com>" in from_header or "bot@example.com>" in from_header
    assert "Монитор" in from_header


def test_from_header_display_name_no_pii_leak_in_detail() -> None:
    """T19c — display name must not appear in NotifyResult.detail."""
    creds = _make_creds(from_name="SensitiveName")
    repo = FakeSmtpCredentialsRepository(creds=creds)
    hp = FakeSmtpHostPolicy()
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo, config_source=FakeConfigSource(),
        clock=FakeClock(), host_policy=hp, url_builder=_URL_BUILDER,
    )
    m = _mock_starttls_smtp()
    m.send_message.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(_make_lot(id=1), _RECIPIENT)
    assert result.ok is False
    assert "SensitiveName" not in result.detail


def test_implicit_tls_happy_path():
    """T20 — port=465: raw TCP, wrap_socket with correct SNI, no STARTTLS command."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=465)
    repo = FakeSmtpCredentialsRepository(creds=creds)
    hp = FakeSmtpHostPolicy(host=_HOST, port=465)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo, config_source=FakeConfigSource(),
        clock=FakeClock(), host_policy=hp, url_builder=_URL_BUILDER,
    )
    lot = _make_lot()
    tls_sock = MagicMock()
    raw_sock = MagicMock()

    with patch("smtplib.SMTP") as mock_smtp_class, \
         patch("ssl.create_default_context") as mssl, \
         patch("socket.create_connection", return_value=raw_sock) as mock_conn:
        mock_smtp = mock_smtp_class.return_value
        mssl.return_value.wrap_socket.return_value = tls_sock
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is True
    assert result.detail == "sent"
    mock_conn.assert_called_once_with((_IP, 465), timeout=10.0)
    _, kw = mssl.return_value.wrap_socket.call_args
    assert kw.get("server_hostname") == _HOST
    assert kw.get("server_hostname") != _IP
    assert mssl.return_value.check_hostname is True
    _, kw2 = mock_smtp_class.call_args
    assert "host" not in kw2 or not kw2.get("host")
    assert mock_smtp.sock is tls_sock
    mock_smtp.getreply.assert_called_once()
    mock_smtp.docmd.assert_not_called()
    mock_smtp.ehlo.assert_any_call(_HOST)
    mock_smtp.login.assert_called_once_with("bot@example.com", "s3cr3t")
    mock_smtp.send_message.assert_called_once()
    assert hp.resolve_called_with == [(_HOST, 465)]


def test_implicit_tls_tls_error_on_wrap():
    """T21 — port=465: SSLError during wrap_socket → retryable=True, detail starts tls_."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=465)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
        config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=FakeSmtpHostPolicy(host=_HOST, port=465),
        url_builder=_URL_BUILDER,
    )
    with patch("smtplib.SMTP"), patch("ssl.create_default_context") as mssl, \
         patch("socket.create_connection"):
        mssl.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError("mismatch")
        result = notifier.send(_make_lot(), _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("tls_")
    assert _RECIPIENT not in result.detail


def test_implicit_tls_connect_timeout():
    """T22 — port=465: TimeoutError during create_connection → retryable=True."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=465)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
        config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=FakeSmtpHostPolicy(host=_HOST, port=465),
        url_builder=_URL_BUILDER,
    )
    with patch("smtplib.SMTP"), patch("ssl.create_default_context"), \
         patch("socket.create_connection", side_effect=TimeoutError("timed out")):
        result = notifier.send(_make_lot(), _RECIPIENT)
    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("connect_error_")
    assert _RECIPIENT not in result.detail


def test_implicit_tls_auth_error():
    """T23 — port=465: SMTPAuthenticationError → retryable=False, detail=auth_failed."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=465)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
        config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=FakeSmtpHostPolicy(host=_HOST, port=465),
        url_builder=_URL_BUILDER,
    )
    m = MagicMock()
    m.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad")
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl, \
         patch("socket.create_connection"):
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(_make_lot(), _RECIPIENT)
    assert result.ok is False
    assert result.retryable is False
    assert result.detail == "auth_failed"
    assert _RECIPIENT not in result.detail
    m.quit.assert_called()


def test_implicit_tls_no_starttls_command_sent():
    """T24 — Invariant: STARTTLS command NEVER sent for port 465 implicit-TLS path."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=465)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
        config_source=FakeConfigSource(), clock=FakeClock(),
        host_policy=FakeSmtpHostPolicy(host=_HOST, port=465),
        url_builder=_URL_BUILDER,
    )
    m = MagicMock()
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl, \
         patch("socket.create_connection"):
        mssl.return_value.wrap_socket.return_value = MagicMock()
        notifier.send(_make_lot(), _RECIPIENT)
    m.docmd.assert_not_called()


@pytest.mark.parametrize("port,use_implicit", [(587, False), (465, True)])
def test_both_tls_paths_send_ok(port, use_implicit):
    """T25 — STARTTLS (587) and implicit TLS (465) both produce ok=True; ADR-021 SNI ok."""
    creds = _make_creds(smtp_host=_HOST, smtp_port=port)
    hp = FakeSmtpHostPolicy(host=_HOST, port=port)
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=FakeSmtpCredentialsRepository(creds=creds),
        config_source=FakeConfigSource(), clock=FakeClock(), host_policy=hp,
        url_builder=_URL_BUILDER,
    )
    m = MagicMock()
    m.docmd.return_value = (220, b"Go ahead")
    m.sock = MagicMock()
    with patch("smtplib.SMTP", return_value=m), \
         patch("ssl.create_default_context") as mssl, \
         patch("socket.create_connection"):
        mssl.return_value.wrap_socket.return_value = MagicMock()
        result = notifier.send(_make_lot(), _RECIPIENT)
    assert result.ok is True
    assert result.detail == "sent"
    if use_implicit:
        m.docmd.assert_not_called()
    else:
        m.docmd.assert_called_once_with("STARTTLS")
    _, kw = mssl.return_value.wrap_socket.call_args
    assert kw.get("server_hostname") == _HOST, (
        f"ADR-021: server_hostname must be {_HOST!r}, got {kw.get('server_hostname')!r}"
    )
    assert kw.get("server_hostname") != _IP, "ADR-021: SNI must not be the pinned IP"


def test_happy_path_send_ok():
    """T1 (mock) — Full SMTP call sequence for STARTTLS path; ADR-021 SNI invariant."""
    lot = _make_lot()
    notifier, repo, hp = _make_notifier()
    m = _mock_starttls_smtp()
    wrapped_sock = MagicMock()
    with patch("smtplib.SMTP", return_value=m) as mock_smtp_class, \
         patch("ssl.create_default_context") as mssl:
        mssl.return_value.wrap_socket.return_value = wrapped_sock
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is True
    assert result.detail == "sent"
    assert result.retryable is False
    mock_smtp_class.assert_called_once_with(host=_IP, port=_PORT, timeout=10.0)
    m.ehlo.assert_any_call(_HOST)
    m.docmd.assert_called_once_with("STARTTLS")
    _, kw = mssl.return_value.wrap_socket.call_args
    assert kw.get("server_hostname") == _HOST
    assert kw.get("server_hostname") != _IP, "ADR-021 invariant: SNI must be hostname, not IP"
    assert mssl.return_value.check_hostname is True
    assert m.file is None
    m.quit.assert_called()
    m.login.assert_called_once_with("bot@example.com", "s3cr3t")
    m.send_message.assert_called_once()
    assert repo._load_called is True
    assert hp.resolve_called_with == [(_HOST, _PORT)]
