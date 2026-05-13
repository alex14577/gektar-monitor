"""SMTP host policy — resolve and validate before connect.

Implements ADR-015: policy-validation (safe host for our environment) lives here
in infra, NOT in the domain ``SmtpCredentials`` Pydantic model.  Domain performs
format-validation only; this module owns DNS resolution and blocklist enforcement.

Security properties guaranteed:
* Fail-closed: if ANY resolved address fails the blocklist the whole call fails.
* TOCTOU mitigation: caller uses the returned ``ResolvedSmtpEndpoint.ip`` for
  ``socket.connect()`` directly — no second ``getaddrinfo()`` inside smtplib.
* DNS-rebinding: multi-record responses are fully validated; one bad address
  poisons the batch (R3-C4, ADR-015, ADR-021).
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import ResolvedSmtpEndpoint

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_BLOCKED_TLDS: frozenset[str] = frozenset(
    {
        "local",
        "internal",
        "lan",
        "corp",
        "home",
        "localdomain",
        "test",
        "example",
        "invalid",
        "localhost",
        # `*.arpa` zones are reverse-DNS / infrastructure (incl. RFC 8375
        # `home.arpa`). No legitimate SMTP host should ever live under .arpa.
        "arpa",
    }
)

_CLOUD_META_V4 = ipaddress.IPv4Address("169.254.169.254")
_CLOUD_META_V6 = ipaddress.IPv6Address("fd00:ec2::254")
_BROADCAST_V4 = ipaddress.IPv4Address("255.255.255.255")

# Resolver type: matches socket.getaddrinfo return shape — a 5-tuple per
# RFC 3493 §6.1 (family, socktype, proto, canonname, sockaddr).
_GetAddrInfoTuple = tuple[Any, Any, Any, Any, Any]
_Resolver = Callable[[str, int], list[_GetAddrInfoTuple]]


# ---------------------------------------------------------------------------
# Protocol (structural typing — testable without importing the concrete class)
# ---------------------------------------------------------------------------


@runtime_checkable
class SmtpHostPolicy(Protocol):
    """Structural interface for SMTP host policy implementations."""

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        """Resolve *host* and validate every returned address against blocklist.

        Returns the first address that passed all checks as a pinned
        ``ResolvedSmtpEndpoint``.  Raises ``SmtpHostPolicyError`` if any address
        is blocked or the host is rejected pre-resolve.  Raises ``ValueError``
        for invalid port numbers.
        """
        ...


# ---------------------------------------------------------------------------
# Default implementation
# ---------------------------------------------------------------------------


class DefaultSmtpHostPolicy:
    """DNS-resolve + IP blocklist enforcement for SMTP host validation.

    Single responsibility: given a ``(host, port)`` pair, produce a
    ``ResolvedSmtpEndpoint`` that is safe to connect to, or raise
    ``SmtpHostPolicyError``.

    Args:
        resolver: Injectable callable with the same signature as
            ``socket.getaddrinfo(host, port, family=AF_UNSPEC,
            type=SOCK_STREAM)``.  When *None* the real ``socket.getaddrinfo``
            is used with ``getaddrinfo_timeout`` applied via
            ``socket.setdefaulttimeout`` (fallback only — real code injects).
        getaddrinfo_timeout: Seconds for the DNS lookup when using the real
            resolver.  Ignored when a custom *resolver* is injected.
    """

    def __init__(
        self,
        *,
        resolver: _Resolver | None = None,
        getaddrinfo_timeout: float = 5.0,
    ) -> None:
        self._resolver = resolver
        self._timeout = getaddrinfo_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        """Resolve *host* and validate; return pinned endpoint or raise."""
        _validate_port(port)
        _reject_pre_resolve(host)

        # IP literal — skip DNS, validate directly.
        literal = _try_parse_ip(host)
        if literal is not None:
            addr = _unwrap_ipv4_mapped(literal)
            _check_address(addr, host)
            family = (
                socket.AF_INET
                if isinstance(addr, ipaddress.IPv4Address)
                else socket.AF_INET6
            )
            return ResolvedSmtpEndpoint(
                ip=str(addr), family=family, port=port, original_host=host
            )

        # Hostname — resolve, then validate every returned address.
        results = self._resolve(host, port)
        if not results:
            raise SmtpHostPolicyError(
                f"smtp host {host!r} returned no addresses from DNS"
            )

        first_ok: ResolvedSmtpEndpoint | None = None
        for record in results:
            # Guard against malformed resolver output. Real getaddrinfo always
            # returns 5-tuples, but an injected resolver could lie — we cannot
            # let an IndexError or TypeError escape unwrapped.
            if not isinstance(record, tuple) or len(record) != 5:
                raise SmtpHostPolicyError(
                    f"smtp host {host!r} DNS returned malformed record"
                )
            af, _socktype, _proto, _canonname, sockaddr = record
            if (
                not isinstance(sockaddr, (tuple, list))
                or not sockaddr
                or not isinstance(sockaddr[0], str)
            ):
                raise SmtpHostPolicyError(
                    f"smtp host {host!r} DNS returned malformed sockaddr"
                )

            try:
                addr_obj = ipaddress.ip_address(sockaddr[0])
            except ValueError as exc:
                raise SmtpHostPolicyError(
                    f"smtp host {host!r} DNS returned unparseable address"
                ) from exc

            addr_obj = _unwrap_ipv4_mapped(addr_obj)
            # Fail-closed: bad address in any record poisons the batch.
            _check_address(addr_obj, host)

            if first_ok is None:
                first_ok = ResolvedSmtpEndpoint(
                    ip=str(addr_obj),
                    family=af,
                    port=port,
                    original_host=host,
                )

        if first_ok is None:
            # Defensive — unreachable given the empty-results check above, but
            # `assert` is stripped under -O, and silently returning is unsafe.
            raise SmtpHostPolicyError(
                f"smtp host {host!r} produced no valid endpoint"
            )
        return first_ok

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve(self, host: str, port: int) -> list[_GetAddrInfoTuple]:
        if self._resolver is not None:
            try:
                return self._resolver(host, port)
            except socket.gaierror as exc:
                raise SmtpHostPolicyError(
                    f"smtp host {host!r}: dns resolution failed"
                ) from exc

        # Real socket path. `socket.getaddrinfo` has no per-call timeout
        # parameter; `socket.setdefaulttimeout` is process-global and not
        # thread-safe. Use a single-shot executor to bound wall-clock — the
        # underlying C-level call may keep running but the policy method
        # returns control to the caller within `self._timeout` seconds.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smtp-dns"
        ) as pool:
            future = pool.submit(
                socket.getaddrinfo,
                host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            try:
                return future.result(timeout=self._timeout)
            except concurrent.futures.TimeoutError as exc:
                raise SmtpHostPolicyError(
                    f"smtp host {host!r}: dns resolution timed out"
                ) from exc
            except socket.gaierror as exc:
                raise SmtpHostPolicyError(
                    f"smtp host {host!r}: dns resolution failed"
                ) from exc


# ---------------------------------------------------------------------------
# Module-level pure helpers (no state — easy to unit-test in isolation)
# ---------------------------------------------------------------------------


def _validate_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise ValueError(f"port {port!r} is out of valid range 1-65535")


def _try_parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return parsed IP address if *host* is a valid literal, else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _reject_pre_resolve(host: str) -> None:
    """Raise SmtpHostPolicyError for hosts that must be rejected without DNS lookup."""
    if not host:
        raise SmtpHostPolicyError("smtp host must not be empty")

    # Tolerate a single trailing dot (FQDN notation) for TLD extraction only.
    stripped = host.rstrip(".")

    # Numeric-only strings that are not valid dotted-decimal IPs (e.g. "0",
    # "3232235521" — packed integer representations) are rejected outright because
    # some OS libc implementations accept them as IPv4 literals.
    if stripped.isdigit():
        raise SmtpHostPolicyError(
            f"smtp host {host!r} rejected: bare integer is not a valid hostname"
        )

    # "localhost" as a plain label — reject before any OS lookup.
    if stripped.lower() == "localhost":
        raise SmtpHostPolicyError(
            f"smtp host {host!r} rejected: 'localhost' is a loopback alias"
        )

    # Blocked TLD suffixes (RFC 6761 / 2606 + common internal conventions).
    lower = stripped.lower()
    parts = lower.rsplit(".", 1)
    tld = parts[-1] if len(parts) > 1 else ""
    if tld in _BLOCKED_TLDS:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} rejected: '.{tld}' TLD is reserved/internal"
        )


def _unwrap_ipv4_mapped(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap ``::ffff:a.b.c.d`` so IPv4 rules apply to the mapped address."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _check_address(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host: str,
) -> None:
    """Raise SmtpHostPolicyError if *addr* is in any blocked range.

    Cloud-metadata endpoints are checked explicitly BEFORE generic link-local so
    the error message is specific (docs/architecture/03-protocols.md §3.3).
    """
    # Cloud metadata — explicit check first for precise error message.
    if addr in (_CLOUD_META_V4, _CLOUD_META_V6):
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to cloud metadata endpoint"
        )

    if addr == _BROADCAST_V4:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to broadcast address"
        )

    if addr.is_loopback:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to loopback address"
        )

    if addr.is_private:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to private IP"
        )

    if addr.is_link_local:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to link-local address"
        )

    if addr.is_multicast:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to multicast address"
        )

    if addr.is_reserved:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to reserved address"
        )

    if addr.is_unspecified:
        raise SmtpHostPolicyError(
            f"smtp host {host!r} resolved to unspecified address (0.0.0.0 / ::)"
        )
