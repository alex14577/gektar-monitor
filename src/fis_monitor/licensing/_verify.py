"""License key verification.

Pure functions only. No I/O, no side effects.
All errors produce INVALID — this module never raises to callers.

Only v2 keys are supported. v1 keys return INVALID with 'unsupported version'.

Verification order per ADR-056:
  1. Split into prefix / payload / sig segments.
  2. base64-decode payload → json.loads (minimal parse, no type assertions).
  3. Reconstruct canonical bytes.
  4. HMAC verify over canonical bytes.
  5. Only after HMAC passes: extract and validate payload field types.
  6. Date-range checks.
"""

import base64
import enum
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
        expires_at: Expiry date from payload, or ``None`` when the key is invalid.
        licensee: Licensee identifier from payload, or ``None`` when unavailable.
    """

    status: LicenseStatus
    expires_at: date | None
    licensee: str | None


_SIG_LENGTH = 32  # HMAC-SHA256 digest bytes


def _decode_v2_raw(key_body: str) -> tuple[dict, bytes]:
    """Decode a v2 key body into a raw payload dict and raw signature bytes.

    Performs only the minimum work needed before HMAC verification:
    base64-decode both segments and json.loads the payload. Does NOT
    validate field types — that is the caller's responsibility after
    HMAC verification succeeds.

    Args:
        key_body: The part of the key string after the ``v2.`` prefix.

    Returns:
        Tuple of (payload dict, raw signature bytes).

    Raises:
        ValueError: If the format is malformed, base64 is invalid, JSON is
            not a dict, or the signature length is not exactly 32 bytes.
    """
    encoded_payload, sep, encoded_sig = key_body.partition(".")
    if not sep or not encoded_payload or not encoded_sig:
        raise ValueError("v2 key body missing '.' separator or empty segment")

    payload = decode_payload(encoded_payload)

    padded_sig = encoded_sig + "=" * (-len(encoded_sig) % 4)
    sig_bytes = base64.urlsafe_b64decode(padded_sig)

    if len(sig_bytes) != _SIG_LENGTH:
        raise ValueError(
            f"Signature length {len(sig_bytes)} != expected {_SIG_LENGTH}"
        )

    return payload, sig_bytes


def _extract_v2_fields(payload: dict) -> tuple[int, date, date, str | None]:
    """Extract and validate typed fields from a v2 payload dict.

    Called only AFTER HMAC verification has passed.

    Args:
        payload: Decoded payload dict (already HMAC-verified).

    Returns:
        Tuple of (v, nbf_date, exp_date, licensee).

    Raises:
        ValueError: If any required field is missing, has the wrong type, or
            cannot be parsed as a date.
        KeyError: If a required key is absent.
    """
    if payload.get("v") != 2:
        raise ValueError("invalid payload structure")
    if not isinstance(payload.get("nbf"), str):
        raise ValueError("invalid payload structure")
    if not isinstance(payload.get("exp"), str):
        raise ValueError("invalid payload structure")
    if not isinstance(payload.get("lic"), str):
        raise ValueError("invalid payload structure")

    nbf_date = date.fromisoformat(payload["nbf"])
    exp_date = date.fromisoformat(payload["exp"])
    licensee: str | None = payload.get("lic")

    return 2, nbf_date, exp_date, licensee


def verify_license(key_str: str, secret: bytes, now: datetime) -> LicenseResult:
    """Verify a license key string.

    Pure function. Never raises. All malformed / tampered / unknown inputs
    produce ``LicenseResult(INVALID, ...)``.

    Only v2 keys are accepted. Any other version prefix (including v1) results
    in INVALID with reason 'unsupported version'.

    Verification order (ADR-056):
      - Signature is verified BEFORE payload field types are extracted.

    Args:
        key_str: License key string in ``<version>.<body>`` format.
        secret: 32-byte HMAC secret used to sign the key.
        now: Current instant (injected; no ``datetime.now()`` calls inside).
            Must be timezone-aware; naive datetime returns INVALID.

    Returns:
        :class:`LicenseResult` with the verification outcome.
    """
    _invalid = LicenseResult(LicenseStatus.INVALID, expires_at=None, licensee=None)

    # SE-3: reject naive datetime
    if now.tzinfo is None:
        return _invalid

    try:
        prefix, _, body = key_str.partition(".")
        if not prefix or not body:
            return _invalid

        if prefix != "v2":
            # Any other version (including v1) is unsupported
            return _invalid

        # Step 1: minimal decode (base64 + json.loads only, no type assertions)
        payload, sig_bytes = _decode_v2_raw(body)

        # Step 2: HMAC verification BEFORE extracting typed fields (ADR-056)
        payload_bytes = _canonical_bytes(payload)
        if not verify_signature(payload_bytes, sig_bytes, secret):
            return _invalid

        # Step 3: extract and validate typed fields (only reachable with valid HMAC)
        _, nbf_date, exp_date, licensee = _extract_v2_fields(payload)

        today = now.date()

        # Anti-rollback: nbf acts as floor
        if today < nbf_date:
            return _invalid

        if today > exp_date:
            return LicenseResult(
                LicenseStatus.EXPIRED, expires_at=exp_date, licensee=licensee
            )

        return LicenseResult(LicenseStatus.VALID, expires_at=exp_date, licensee=licensee)

    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return _invalid
