"""Integration tests for SmtpEmailNotifier.

Uses ``unittest.mock.patch`` on ``smtplib.SMTP`` — no real network calls.

Fake collaborators:
* ``FakeSmtpCredentialsRepository`` — in-memory, single credential slot.
* ``FakeSmtpHostPolicy`` — returns pinned ``ResolvedSmtpEndpoint`` for any input.

All tests assert NO PII (recipient email) appears in ``NotifyResult.detail``.
"""

from __future__ import annotations

import re
import smtplib
import socket
import ssl
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
from fis_monitor.infra.smtp.email_notifier import SmtpEmailNotifier

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_RECIPIENT = "alice@example.com"
_RECIPIENT2 = "bob@example.org"
_IP = "1.2.3.4"
_HOST = "smtp.test.example.com"
_PORT = 587

_MESSAGE_ID_PATTERN = re.compile(
    r"^<\d+\.email\.[0-9a-f]{16}@fis-monitor\.local>$"
)

# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class FakeSmtpCredentialsRepository:
    """In-memory SmtpCredentialsRepository for tests."""

    _loaded: bool = False

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
    """Minimal ConfigSource stub — not exercised by notifier yet."""

    def get(self):
        return {}

    def subscribe(self, cb):
        class _Handle:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return _Handle()


class FakeClock:
    """Minimal Clock stub — not exercised by notifier yet."""

    def now(self):
        from datetime import UTC, datetime

        return datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


class FakeSmtpHostPolicy:
    """Fake SmtpHostPolicy — always returns a pinned endpoint."""

    def __init__(
        self,
        *,
        ip: str = _IP,
        host: str = _HOST,
        port: int = _PORT,
        raise_exc: Exception | None = None,
    ) -> None:
        self._endpoint = ResolvedSmtpEndpoint(
            ip=ip,
            family=socket.AF_INET,
            port=port,
            original_host=host,
        )
        self._raise_exc = raise_exc
        self.resolve_called_with: list[tuple[str, int]] = []

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        self.resolve_called_with.append((host, port))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._endpoint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_creds(**overrides) -> SmtpCredentials:
    defaults = {
        "smtp_user": "bot@example.com",
        "smtp_password": SecretStr("s3cr3t"),
        "smtp_host": _HOST,
        "smtp_port": _PORT,
        "use_default": True,
    }
    defaults.update(overrides)
    return SmtpCredentials(**defaults)


def _make_lot(**overrides) -> LotPublicDTO:
    from datetime import UTC, datetime
    defaults = {
        "id": 99,
        "cadastral_no": "27:23:0040000:0099",
        "area_sqm": 5000,
        "region": "Тестовый регион",
        "municipality": "Тестовый город",
        "land_category": "test-category",
        "permitted_use": "test-use",
        "ogv": "test-ogv",
        "status": "Свободен",
        "date_create": datetime(2026, 5, 1, tzinfo=UTC),
        "date_update": datetime(2026, 5, 13, tzinfo=UTC),
        "lat": 55.75,
        "lon": 37.61,
        "has_boundaries": True,
        "raw_json": {},
        "parser_version": 1,
        "first_seen": datetime(2026, 5, 1, tzinfo=UTC),
        "last_seen": datetime(2026, 5, 13, tzinfo=UTC),
        "detail_fetched_at": datetime(2026, 5, 13, tzinfo=UTC),
        "enrichment_status": "done",
        "last_seen_at": datetime(2026, 5, 13, tzinfo=UTC),
        "is_active": True,
        "inactive_reason": None,
        "inactive_since": None,
        "inactive_confirmed_at": None,
        "age_seconds": 60,
        "tier": "match",
        "freshness": "hot",
    }
    defaults.update(overrides)
    return LotPublicDTO(**defaults)


_NO_CREDS = object()  # sentinel: repo.load() returns None


def _make_notifier(
    *,
    creds: SmtpCredentials | None | object = _NO_CREDS,
    host_policy: FakeSmtpHostPolicy | None = None,
    connect_timeout: float = 10.0,
) -> tuple[SmtpEmailNotifier, FakeSmtpCredentialsRepository, FakeSmtpHostPolicy]:
    """Build notifier with fakes; return (notifier, repo, host_policy).

    Pass ``creds=None`` to simulate a missing-credentials repo (repo.load() → None).
    Omit ``creds`` to use default test credentials.
    """
    resolved_creds: SmtpCredentials | None = (
        _make_creds() if creds is _NO_CREDS else creds  # type: ignore[assignment]
    )
    repo = FakeSmtpCredentialsRepository(creds=resolved_creds)
    hp = host_policy or FakeSmtpHostPolicy()
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo,
        config_source=FakeConfigSource(),
        clock=FakeClock(),
        host_policy=hp,
        connect_timeout=connect_timeout,
    )
    return notifier, repo, hp


# ---------------------------------------------------------------------------
# T1 — Happy path: full send, verify smtplib call sequence
# ---------------------------------------------------------------------------


def test_happy_path_send_ok():
    """T1 — verifies SMTP connect-by-IP, EHLO with original_host, manual STARTTLS,
    wrap_socket with server_hostname=original_host (NOT ip), login, send_message."""
    lot = _make_lot()
    notifier, repo, hp = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()

    wrapped_sock = MagicMock()

    with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_class, \
         patch("ssl.create_default_context") as mock_ssl_ctx:

        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = wrapped_sock

        result = notifier.send(lot, _RECIPIENT)

    # --- Result ---
    assert result.ok is True
    assert result.detail == "sent"
    assert result.retryable is False

    # --- smtplib.SMTP called with pinned IP, not hostname ---
    mock_smtp_class.assert_called_once_with(
        host=_IP, port=_PORT, timeout=10.0
    )

    # --- First EHLO with original_host ---
    mock_smtp_instance.ehlo.assert_any_call(_HOST)

    # --- STARTTLS command sent manually ---
    mock_smtp_instance.docmd.assert_called_once_with("STARTTLS")

    # --- wrap_socket was called with server_hostname=original_host (NOT ip) ---
    # Note: smtp.sock was reassigned by wrap_socket; check the keyword arg only.
    mock_ctx.wrap_socket.assert_called_once()
    _, kw = mock_ctx.wrap_socket.call_args
    assert kw.get("server_hostname") == _HOST, (
        f"Expected server_hostname={_HOST!r}, got {kw.get('server_hostname')!r}"
    )
    assert kw.get("server_hostname") != _IP, (
        "ADR-021 invariant: server_hostname must be the DNS hostname, not the pinned IP"
    )
    assert mock_ctx.check_hostname is True
    # file invalidated
    assert mock_smtp_instance.file is None

    # --- quit() was called ---
    mock_smtp_instance.quit.assert_called()

    # --- Login with plaintext password ---
    mock_smtp_instance.login.assert_called_once_with("bot@example.com", "s3cr3t")

    # --- send_message called ---
    mock_smtp_instance.send_message.assert_called_once()

    # --- Repo.load was called (coverage of fake) ---
    assert repo._load_called is True

    # --- host_policy.resolve_and_check was called ---
    assert hp.resolve_called_with == [(_HOST, _PORT)]


# ---------------------------------------------------------------------------
# T2 — Message-ID format
# ---------------------------------------------------------------------------


def test_message_id_format():
    """T2 — Message-ID matches <lot_id.email.16hexchars@fis-monitor.local>."""
    lot = _make_lot(id=42)
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()

    captured_msg: list[EmailMessage] = []

    def capture_send(msg):
        captured_msg.append(msg)

    mock_smtp_instance.send_message.side_effect = capture_send

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        notifier.send(lot, _RECIPIENT)

    assert captured_msg, "send_message was not called"
    mid = captured_msg[0]["Message-ID"]
    assert _MESSAGE_ID_PATTERN.match(mid), f"Bad Message-ID: {mid!r}"
    assert mid.startswith("<42.email.")
    # ADR-019 R4-C5 PII invariant: recipient must not appear in Message-ID
    assert _RECIPIENT not in mid
    assert "alice" not in mid
    assert "example.com" not in mid


# ---------------------------------------------------------------------------
# T3 — Same recipient → same Message-ID (determinism)
# ---------------------------------------------------------------------------


def test_same_recipient_same_message_id():
    """T3 — Same lot + recipient always produces identical Message-ID."""
    lot = _make_lot(id=7)
    notifier, _, _ = _make_notifier()

    mid1 = notifier._make_message_id(lot.id, _RECIPIENT)
    mid2 = notifier._make_message_id(lot.id, _RECIPIENT)
    assert mid1 == mid2


# ---------------------------------------------------------------------------
# T4 — Different recipient → different hash
# ---------------------------------------------------------------------------


def test_different_recipient_different_message_id():
    """T4 — Different recipient yields different hash portion of Message-ID."""
    lot = _make_lot(id=7)
    notifier, _, _ = _make_notifier()

    mid1 = notifier._make_message_id(lot.id, _RECIPIENT)
    mid2 = notifier._make_message_id(lot.id, _RECIPIENT2)
    assert mid1 != mid2

    # Extract hash portions and compare
    h1 = mid1.split(".")[2].split("@")[0]
    h2 = mid2.split(".")[2].split("@")[0]
    assert h1 != h2


# ---------------------------------------------------------------------------
# T5 — STARTTLS refused (code != 220)
# ---------------------------------------------------------------------------


def test_starttls_refused():
    """T5 — docmd("STARTTLS") returns non-220 → NotifyResult(ok=False, retryable=True)."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (500, b"Not supported")
    mock_smtp_instance.sock = MagicMock()

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context"):
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("starttls")
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T6 — SMTPAuthenticationError
# ---------------------------------------------------------------------------


def test_smtp_auth_error():
    """T6 — SMTPAuthenticationError → ok=False, retryable=False, detail=auth_failed."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication credentials invalid"
    )

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is False
    assert result.detail == "auth_failed"
    assert _RECIPIENT not in result.detail
    mock_smtp_instance.quit.assert_called()


# ---------------------------------------------------------------------------
# T7 — socket.timeout
# ---------------------------------------------------------------------------


def test_socket_timeout():
    """T7 — socket.timeout mid-send → ok=False, retryable=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.send_message.side_effect = TimeoutError("timed out")

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is True
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T8 — SMTPRecipientsRefused
# ---------------------------------------------------------------------------


def test_smtp_recipients_refused():
    """T8 — SMTPRecipientsRefused → ok=False, retryable=False, detail=recipient_refused."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {_RECIPIENT: (550, b"User unknown")}
    )

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is False
    assert result.detail == "recipient_refused"
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T9 — SmtpHostPolicyError propagates (not caught)
# ---------------------------------------------------------------------------


def test_smtp_host_policy_error_propagates():
    """T9 — SmtpHostPolicyError from host_policy.resolve_and_check is NOT caught."""
    lot = _make_lot()
    hp = FakeSmtpHostPolicy(
        raise_exc=SmtpHostPolicyError("smtp host 'evil.corp' rejected")
    )
    notifier, _, _ = _make_notifier(host_policy=hp)

    with pytest.raises(SmtpHostPolicyError):
        notifier.send(lot, _RECIPIENT)


# ---------------------------------------------------------------------------
# T10 — No PII in detail for all error cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPAuthenticationError(535, b"bad creds"),
        smtplib.SMTPRecipientsRefused({_RECIPIENT: (550, b"User unknown")}),
        smtplib.SMTPServerDisconnected("disconnected"),
        smtplib.SMTPResponseException(421, b"Service not available"),
        smtplib.SMTPResponseException(550, b"User unknown"),
        TimeoutError("timed out"),
        ConnectionError("connection refused"),
    ],
)
def test_no_pii_in_detail(exc):
    """T10 — recipient email never appears in NotifyResult.detail."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    # Raise the exception at login or send_message depending on type
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        mock_smtp_instance.login.side_effect = exc
    else:
        mock_smtp_instance.send_message.side_effect = exc

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert _RECIPIENT not in result.detail, (
        f"PII leak: recipient found in detail={result.detail!r} for exc={exc!r}"
    )
    # Also verify email domain substring not in detail
    assert "alice" not in result.detail
    assert "example.com" not in result.detail


# ---------------------------------------------------------------------------
# T11 — test(recipient): Message-ID has lot_id=0, subject contains "test"
# ---------------------------------------------------------------------------


def test_test_method_message_id_and_subject():
    """T11 — test() uses lot_id=0 in Message-ID, subject contains 'test'."""
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()

    captured_msg: list[EmailMessage] = []

    def capture_send(msg):
        captured_msg.append(msg)

    mock_smtp_instance.send_message.side_effect = capture_send

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.test(_RECIPIENT)

    assert result.ok is True
    assert captured_msg, "send_message was not called"
    msg = captured_msg[0]

    # lot_id=0 in Message-ID
    mid = msg["Message-ID"]
    assert mid.startswith("<0.email."), f"Expected lot_id=0 in Message-ID, got: {mid!r}"

    # subject contains "test"
    subject = msg["Subject"]
    assert "test" in subject.lower(), f"Expected 'test' in subject, got: {subject!r}"


# ---------------------------------------------------------------------------
# T12 — ssl.SSLCertVerificationError → retryable=True, detail starts with "tls_"
# ---------------------------------------------------------------------------


def test_ssl_cert_verification_error():
    """T12 — SSLCertVerificationError from wrap_socket → retryable=True, no PII."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.side_effect = ssl.SSLCertVerificationError(
            "certificate verify failed: hostname mismatch"
        )
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is True
    assert result.detail.startswith("tls_")
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T13 — SMTPServerDisconnected → retryable=True
# ---------------------------------------------------------------------------


def test_smtp_server_disconnected():
    """T13 — SMTPServerDisconnected → ok=False, retryable=True."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPServerDisconnected(
        "Connection unexpectedly closed"
    )

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is True
    assert result.detail == "server_disconnected"


# ---------------------------------------------------------------------------
# T14 — Full fake coverage (orchestrator-playbook rule 6)
# ---------------------------------------------------------------------------


def test_all_fake_methods_called_in_happy_path():
    """T14 — full happy-path send exercises ALL methods of both fake collaborators.

    Covers: FakeSmtpCredentialsRepository.load(), FakeSmtpHostPolicy.resolve_and_check().
    """
    lot = _make_lot(id=55)
    creds = _make_creds(smtp_host="smtp.production.example.com", smtp_port=587)
    repo = FakeSmtpCredentialsRepository(creds=creds)
    hp = FakeSmtpHostPolicy(host="smtp.production.example.com")
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo,
        config_source=FakeConfigSource(),
        clock=FakeClock(),
        host_policy=hp,
        connect_timeout=5.0,
    )

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is True

    # FakeSmtpCredentialsRepository.load() was called
    assert repo._load_called is True, "load() was not called on the repo"

    # FakeSmtpHostPolicy.resolve_and_check() was called with correct args
    assert len(hp.resolve_called_with) == 1
    called_host, called_port = hp.resolve_called_with[0]
    assert called_host == "smtp.production.example.com"
    assert called_port == 587


# ---------------------------------------------------------------------------
# T15 — No credentials → ok=False, retryable=False
# ---------------------------------------------------------------------------


def test_no_credentials():
    """T15 — When repo.load() returns None, send returns an error result."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier(creds=None)

    with patch("smtplib.SMTP") as mock_smtp_class:
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is False
    # SMTP should never have been created
    mock_smtp_class.assert_not_called()
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T16 — SMTPResponseException 4xx retryable, 5xx not retryable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_retryable",
    [
        (421, True),   # 4xx → retryable
        (450, True),   # 4xx → retryable
        (550, False),  # 5xx → NOT retryable
        (554, False),  # 5xx → NOT retryable
    ],
)
def test_smtp_response_exception_retryable(code, expected_retryable):
    """T16 — SMTPResponseException retryable based on code."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPResponseException(
        code, b"Error message"
    )

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    assert result.ok is False
    assert result.retryable is expected_retryable
    assert result.detail == f"smtp_{code}"
    assert _RECIPIENT not in result.detail


# ---------------------------------------------------------------------------
# T17 — Constructor accepts canon signature (BLOCKER 1)
# ---------------------------------------------------------------------------


def test_constructor_accepts_canon_signature():
    """T17 — SmtpEmailNotifier(smtp_creds_repo=, config_source=, clock=,
    host_policy=) matches build_container §4.2 — no TypeError."""
    repo = FakeSmtpCredentialsRepository()
    hp = FakeSmtpHostPolicy()
    config_source = FakeConfigSource()
    clock = FakeClock()
    # Must not raise TypeError
    notifier = SmtpEmailNotifier(
        smtp_creds_repo=repo,
        config_source=config_source,
        clock=clock,
        host_policy=hp,
    )
    assert notifier.channel_id == "email"


# ---------------------------------------------------------------------------
# T18 — quit() raises → close() fallback is called
# ---------------------------------------------------------------------------


def test_quit_failure_calls_close():
    """T18 — When smtp.quit() raises SMTPException in finally, smtp.close() is called."""
    lot = _make_lot()
    notifier, _, _ = _make_notifier()

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.docmd.return_value = (220, b"Go ahead")
    mock_smtp_instance.sock = MagicMock()
    mock_smtp_instance.quit.side_effect = smtplib.SMTPServerDisconnected("already gone")

    with patch("smtplib.SMTP", return_value=mock_smtp_instance), \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_ctx = MagicMock()
        mock_ssl_ctx.return_value = mock_ctx
        mock_ctx.wrap_socket.return_value = MagicMock()
        result = notifier.send(lot, _RECIPIENT)

    # The send itself succeeded — quit failure is swallowed in finally
    assert result.ok is True
    mock_smtp_instance.quit.assert_called()
    mock_smtp_instance.close.assert_called()
