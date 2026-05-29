"""Shared test infrastructure for the licensing subsystem.

Provides:
- test_secret: fixed 32-byte secret fixture (NOT _assemble_secret())
- make_v2_key: helper to generate v2 license key strings for tests
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


def make_v2_key(
    nbf: date,
    exp: date,
    secret: bytes,
    lic: str = "interactive",
) -> str:
    """Generate a v2 license key string for use in tests.

    Format: ``v2.<base64url_payload>.<base64url_sig>``

    Uses production codec (_canonical_bytes, encode_payload, sign) so that
    any change to serialization is reflected here automatically.

    Args:
        nbf: Not-before date.
        exp: Expiry date.
        secret: 32-byte HMAC secret.
        lic: Licensee identifier (default "interactive"; override for negative tests).

    Returns:
        License key string in ``v2.<payload>.<sig>`` format.
    """
    payload: dict[str, object] = {
        "v": 2,
        "nbf": nbf.isoformat(),
        "exp": exp.isoformat(),
        "lic": lic,
    }
    encoded_payload = encode_payload(payload)
    sig = sign(_canonical_bytes(payload), secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"v2.{encoded_payload}.{encoded_sig}"
