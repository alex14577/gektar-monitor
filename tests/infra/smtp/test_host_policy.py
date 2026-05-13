"""Tests for DefaultSmtpHostPolicy.resolve_and_check.

Coverage: every blocklist category (architecture.md §3.3), DNS-rebinding,
happy path, IP literals, TLD blocklist, edge-case pre-resolve rejects,
port validation, and DNS failure wrapping.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import ResolvedSmtpEndpoint
from fis_monitor.infra.smtp.host_policy import DefaultSmtpHostPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AF_INET = socket.AF_INET
SOCK_STREAM = socket.SOCK_STREAM


def _resolver_for(ip: str) -> callable:
    """Return a fake resolver that always yields a single AF_INET record."""

    def _resolve(host: str, port: int) -> list[tuple]:
        return [(AF_INET, SOCK_STREAM, 0, "", (ip, port))]

    return _resolve


def _resolver_multi(*ips: str) -> callable:
    """Return a fake resolver that yields one AF_INET record per IP."""

    def _resolve(host: str, port: int) -> list[tuple]:
        return [(AF_INET, SOCK_STREAM, 0, "", (ip, port)) for ip in ips]

    return _resolve


def _policy(ip: str) -> DefaultSmtpHostPolicy:
    return DefaultSmtpHostPolicy(resolver=_resolver_for(ip))


# ---------------------------------------------------------------------------
# 1. Loopback (127.0.0.1)
# ---------------------------------------------------------------------------


def test_loopback_v4_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("127.0.0.1").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 2-4. RFC1918 private ranges
# ---------------------------------------------------------------------------


def test_rfc1918_10_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("10.0.0.1").resolve_and_check("smtp.example.com", 587)


def test_rfc1918_172_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("172.16.1.1").resolve_and_check("smtp.example.com", 587)


def test_rfc1918_192_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("192.168.1.1").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 5. Cloud metadata (169.254.169.254) — must mention "metadata" in message
# ---------------------------------------------------------------------------


def test_cloud_metadata_v4_raises_with_metadata_in_message() -> None:
    with pytest.raises(SmtpHostPolicyError, match="metadata"):
        _policy("169.254.169.254").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 6. Link-local non-metadata
# ---------------------------------------------------------------------------


def test_link_local_non_meta_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("169.254.5.5").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 7. IPv6 loopback (::1)
# ---------------------------------------------------------------------------


def test_loopback_v6_raises() -> None:
    def _resolve(host: str, port: int) -> list[tuple]:
        return [(socket.AF_INET6, SOCK_STREAM, 0, "", ("::1", port, 0, 0))]

    policy = DefaultSmtpHostPolicy(resolver=_resolve)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 8. RFC4193 (fc00::1)
# ---------------------------------------------------------------------------


def test_rfc4193_private_v6_raises() -> None:
    def _resolve(host: str, port: int) -> list[tuple]:
        return [(socket.AF_INET6, SOCK_STREAM, 0, "", ("fc00::1", port, 0, 0))]

    policy = DefaultSmtpHostPolicy(resolver=_resolve)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 9. IPv6 link-local (fe80::1)
# ---------------------------------------------------------------------------


def test_link_local_v6_raises() -> None:
    def _resolve(host: str, port: int) -> list[tuple]:
        return [(socket.AF_INET6, SOCK_STREAM, 0, "", ("fe80::1", port, 0, 0))]

    policy = DefaultSmtpHostPolicy(resolver=_resolve)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 10. IPv4-mapped IPv6 private (::ffff:10.0.0.1)
# ---------------------------------------------------------------------------


def test_ipv4_mapped_private_raises() -> None:
    def _resolve(host: str, port: int) -> list[tuple]:
        # Some systems return the mapped form; simulate it.
        return [(socket.AF_INET6, SOCK_STREAM, 0, "", ("::ffff:10.0.0.1", port, 0, 0))]

    policy = DefaultSmtpHostPolicy(resolver=_resolve)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 11. Unspecified (0.0.0.0)
# ---------------------------------------------------------------------------


def test_unspecified_v4_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("0.0.0.0").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 12. Broadcast (255.255.255.255)
# ---------------------------------------------------------------------------


def test_broadcast_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("255.255.255.255").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 13. Multicast (224.0.0.1)
# ---------------------------------------------------------------------------


def test_multicast_raises() -> None:
    with pytest.raises(SmtpHostPolicyError):
        _policy("224.0.0.1").resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 14. DNS-rebinding: two addresses, first OK second blocked → must fail-closed
# ---------------------------------------------------------------------------


def test_dns_rebinding_fails_closed() -> None:
    policy = DefaultSmtpHostPolicy(resolver=_resolver_multi("1.2.3.4", "10.0.0.1"))
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 15. Happy path: public IP passes and returns correct ResolvedSmtpEndpoint
# ---------------------------------------------------------------------------


def test_happy_path_returns_resolved_endpoint() -> None:
    policy = DefaultSmtpHostPolicy(resolver=_resolver_for("8.8.8.8"))
    result = policy.resolve_and_check("smtp.example.com", 587)
    assert isinstance(result, ResolvedSmtpEndpoint)
    assert result.ip == "8.8.8.8"
    assert result.original_host == "smtp.example.com"
    assert result.port == 587
    assert result.family == AF_INET


# ---------------------------------------------------------------------------
# 16. IP literal input — resolver must NOT be called
# ---------------------------------------------------------------------------


def test_ip_literal_skips_resolver() -> None:
    mock_resolver = MagicMock()
    policy = DefaultSmtpHostPolicy(resolver=mock_resolver)
    result = policy.resolve_and_check("8.8.8.8", 25)
    mock_resolver.assert_not_called()
    assert result.ip == "8.8.8.8"
    assert result.original_host == "8.8.8.8"


# ---------------------------------------------------------------------------
# 17. IP literal private — caught pre-resolve (no resolver call needed)
# ---------------------------------------------------------------------------


def test_ip_literal_private_raises() -> None:
    mock_resolver = MagicMock()
    policy = DefaultSmtpHostPolicy(resolver=mock_resolver)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check("10.0.0.1", 587)
    mock_resolver.assert_not_called()


# ---------------------------------------------------------------------------
# 18. TLD blocklist — parametrized, must reject before calling resolver
# ---------------------------------------------------------------------------

_BLOCKED_TLD_HOSTS = [
    "smtp.local",
    "mail.internal",
    "host.lan",
    "box.corp",
    "foo.home",
    "x.test",
    "x.example",
    "x.invalid",
    "x.localdomain",
    "x.localhost",
    # trailing dot variants
    "smtp.local.",
    "mail.internal.",
    # mixed case
    "smtp.LOCAL",
    "mail.INTERNAL",
    "host.LAN",
    # RFC 8375 home.arpa + general *.arpa infrastructure zone
    "mail.home.arpa",
    "host.arpa",
    "x.arpa.",
]


@pytest.mark.parametrize("host", _BLOCKED_TLD_HOSTS)
def test_blocked_tld_raises_pre_resolve(host: str) -> None:
    mock_resolver = MagicMock()
    policy = DefaultSmtpHostPolicy(resolver=mock_resolver)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check(host, 587)
    mock_resolver.assert_not_called()


# ---------------------------------------------------------------------------
# 19. localhost / "0" / integer-IP — rejected pre-resolve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "0", "3232235521"])
def test_pre_resolve_edge_cases_raise(host: str) -> None:
    mock_resolver = MagicMock()
    policy = DefaultSmtpHostPolicy(resolver=mock_resolver)
    with pytest.raises(SmtpHostPolicyError):
        policy.resolve_and_check(host, 587)
    mock_resolver.assert_not_called()


# ---------------------------------------------------------------------------
# 20. Invalid port — raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_port_raises_value_error(port: int) -> None:
    policy = DefaultSmtpHostPolicy(resolver=MagicMock())
    with pytest.raises(ValueError):
        policy.resolve_and_check("smtp.example.com", port)


# ---------------------------------------------------------------------------
# 21. DNS failure — resolver raises gaierror → SmtpHostPolicyError (wrapped)
# ---------------------------------------------------------------------------


def test_dns_failure_wrapped_as_smtp_host_policy_error() -> None:
    def _failing_resolver(host: str, port: int) -> list[tuple]:
        raise socket.gaierror("Name or service not known")

    policy = DefaultSmtpHostPolicy(resolver=_failing_resolver)
    with pytest.raises(SmtpHostPolicyError) as exc_info:
        policy.resolve_and_check("smtp.example.com", 587)

    # Message must be generic — no raw gaierror args leaked.
    msg = str(exc_info.value)
    assert "gaierror" not in msg.lower()
    assert "smtp.example.com" in msg


# ---------------------------------------------------------------------------
# 21b. DNS-rebinding bad-first — bad address as FIRST record must fail-closed
# ---------------------------------------------------------------------------


def test_dns_rebinding_bad_first_fails_closed() -> None:
    def _resolver(host: str, port: int):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port)),
        ]

    policy = DefaultSmtpHostPolicy(resolver=_resolver)
    with pytest.raises(SmtpHostPolicyError, match="private"):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 21c. Malformed resolver output — must not leak IndexError / TypeError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_record",
    [
        ("only", "three", "items"),                           # wrong length
        (socket.AF_INET, 0, 0, "", ()),                       # empty sockaddr
        (socket.AF_INET, 0, 0, "", (123, 587)),               # non-str ip
        "not-a-tuple",                                        # wrong type
        (socket.AF_INET, 0, 0, "", object()),                 # non-subscriptable sockaddr
    ],
)
def test_malformed_resolver_record_wrapped(bad_record) -> None:
    def _resolver(host: str, port: int):
        return [bad_record]

    policy = DefaultSmtpHostPolicy(resolver=_resolver)
    with pytest.raises(SmtpHostPolicyError, match="malformed"):
        policy.resolve_and_check("smtp.example.com", 587)


# ---------------------------------------------------------------------------
# 21d. SmtpHostPolicy Protocol is runtime_checkable
# ---------------------------------------------------------------------------


def test_protocol_is_runtime_checkable() -> None:
    from fis_monitor.infra.smtp.host_policy import SmtpHostPolicy

    policy = DefaultSmtpHostPolicy(resolver=MagicMock())
    assert isinstance(policy, SmtpHostPolicy)


# ---------------------------------------------------------------------------
# 22. Real-resolver smoke test (skipped in CI — hits real DNS)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="hits real DNS — run manually only")
def test_real_resolver_smoke_gmail() -> None:
    policy = DefaultSmtpHostPolicy()
    result = policy.resolve_and_check("smtp.gmail.com", 587)
    assert result.original_host == "smtp.gmail.com"
    assert result.port == 587
    assert result.ip  # non-empty
