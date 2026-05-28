"""License key verification.

Pure functions only. No I/O, no side effects.
All errors produce INVALID — this module never raises to callers.
"""

import base64
import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from fis_monitor.licensing._codec import _canonical_bytes, decode_payload
from fis_monitor.licensing._hmac import verify_signature


class LicenseStatus(enum.Enum):
    """Possible outcomes of license verification."""

    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True)
class LicenseResult:
    """Result returned by :func:`verify_license`.

    Attributes:
        status: Verification outcome.
        expires_at: Expiry date from payload, or ``None`` for perpetual licenses
            or when the key is invalid.
        licensee: Licensee identifier from payload, or ``None`` when unavailable.
    """

    status: LicenseStatus
    expires_at: date | None
    licensee: str | None


# Decoder contract: receives the key body (everything after "v1."),
# returns (payload_dict, sig_bytes).  Raises ValueError on malformed input.
Decoder = Callable[[str], tuple[dict, bytes]]

_SIG_LENGTH = 32  # HMAC-SHA256 digest bytes


def _decode_v1(key_body: str) -> tuple[dict, bytes]:
    """Decode a v1 key body ``<b64_payload>.<b64_sig>`` into components.

    Args:
        key_body: The part of the key string after the ``v1.`` prefix.

    Returns:
        Tuple of (payload dict, raw signature bytes).

    Raises:
        ValueError: If the format is malformed, base64 is invalid, or the
            signature length is not exactly 32 bytes.
    """
    encoded_payload, sep, encoded_sig = key_body.partition(".")
    if not sep or not encoded_payload or not encoded_sig:
        raise ValueError("v1 key body missing '.' separator or empty segment")

    payload = decode_payload(encoded_payload)

    padded_sig = encoded_sig + "=" * (-len(encoded_sig) % 4)
    sig_bytes = base64.urlsafe_b64decode(padded_sig)

    if len(sig_bytes) != _SIG_LENGTH:
        raise ValueError(
            f"Signature length {len(sig_bytes)} != expected {_SIG_LENGTH}"
        )

    return payload, sig_bytes


_DECODER_REGISTRY: dict[str, Decoder] = {
    "v1": _decode_v1,
}


def _dispatch_decoder(version_prefix: str) -> Decoder | None:
    """Return the decoder for *version_prefix*, or ``None`` if unknown.

    Args:
        version_prefix: Version string extracted from the key (e.g. ``"v1"``).

    Returns:
        Callable decoder, or ``None`` for unrecognised versions.
    """
    return _DECODER_REGISTRY.get(version_prefix)


def verify_license(key_str: str, secret: bytes, now: datetime) -> LicenseResult:
    """Verify a license key string.

    Pure function. Never raises. All malformed / tampered / unknown inputs
    produce ``LicenseResult(INVALID, ...)``.

    Args:
        key_str: License key string in ``<version>.<body>`` format.
        secret: 32-byte HMAC secret used to sign the key.
        now: Current instant (injected; no ``datetime.now()`` calls inside).

    Returns:
        :class:`LicenseResult` with the verification outcome.
    """
    _invalid = LicenseResult(LicenseStatus.INVALID, expires_at=None, licensee=None)

    try:
        prefix, _, body = key_str.partition(".")
        if not prefix or not body:
            return _invalid

        decoder = _dispatch_decoder(prefix)
        if decoder is None:
            return _invalid

        payload, sig_bytes = decoder(body)

        payload_bytes = _canonical_bytes(payload)
        if not verify_signature(payload_bytes, sig_bytes, secret):
            return _invalid

        iat_raw = payload.get("iat")
        if not isinstance(iat_raw, str):
            return _invalid
        iat_date = date.fromisoformat(iat_raw)

        exp_raw = payload.get("exp")
        exp_date = date.fromisoformat(exp_raw) if isinstance(exp_raw, str) else None

        licensee_raw = payload.get("lic")
        licensee = licensee_raw if isinstance(licensee_raw, str) else None

        today = now.date()

        if today < iat_date:
            return _invalid

        if exp_date is not None and today > exp_date:
            return LicenseResult(
                LicenseStatus.EXPIRED, expires_at=exp_date, licensee=licensee
            )

        return LicenseResult(LicenseStatus.VALID, expires_at=exp_date, licensee=licensee)

    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return _invalid
