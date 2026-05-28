"""Shared test infrastructure for the licensing subsystem.

Provides:
- test_secret: fixed 32-byte secret fixture (NOT _assemble_secret())
- make_key: helper to generate v1 license key strings for tests
"""

import base64
from datetime import date

import pytest

from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
from fis_monitor.licensing._hmac import sign

# Fixed 32-byte test secret — never use in production
_TEST_SECRET: bytes = (
    b"\x01\x02\x03\x04\x05\x06\x07\x08"
    b"\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
    b"\x11\x12\x13\x14\x15\x16\x17\x18"
    b"\x19\x1a\x1b\x1c\x1d\x1e\x1f\x20"
)


@pytest.fixture
def test_secret() -> bytes:
    """Fixed 32-byte test secret for licensing tests."""
    return _TEST_SECRET


def make_key(
    licensee: str,
    iat: date,
    exp: date | None,
    secret: bytes,
) -> str:
    """Generate a v1 license key string for use in tests.

    Format: ``v1.<base64url_payload>.<base64url_sig>``

    Uses production codec (_canonical_bytes, encode_payload, sign) so that
    any change to serialization is reflected here automatically.

    Args:
        licensee: Licensee identifier string.
        iat: Issued-at date.
        exp: Expiry date, or ``None`` for no expiry.
        secret: 32-byte HMAC secret.

    Returns:
        License key string in ``v1.<payload>.<sig>`` format.
    """
    payload: dict[str, object] = {
        "v": 1,
        "iat": iat.isoformat(),
        "lic": licensee,
    }
    if exp is not None:
        payload["exp"] = exp.isoformat()

    encoded_payload = encode_payload(payload)
    sig = sign(_canonical_bytes(payload), secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

    return f"v1.{encoded_payload}.{encoded_sig}"
