"""Codec for license payload serialization.

Provides deterministic base64url encoding (no padding) of JSON payloads.
Pure functions, no I/O, no side effects.
"""

import base64
import binascii
import json


def _canonical_bytes(payload: dict) -> bytes:
    """Serialize payload dict to canonical JSON bytes (deterministic, sort_keys).

    Args:
        payload: Arbitrary JSON-serializable dict.

    Returns:
        UTF-8 encoded canonical JSON bytes.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_payload(payload: dict) -> str:
    """Serialize payload dict to base64url string (no padding).

    Args:
        payload: Arbitrary JSON-serializable dict.

    Returns:
        Base64url-encoded string without ``=`` padding characters.
    """
    encoded = base64.urlsafe_b64encode(_canonical_bytes(payload))
    return encoded.rstrip(b"=").decode("ascii")


def decode_payload(encoded: str) -> dict:
    """Decode base64url string to dict.

    Args:
        encoded: Base64url string (with or without ``=`` padding).

    Returns:
        Deserialized dict.

    Raises:
        ValueError: If ``encoded`` is not valid base64, does not contain
            valid JSON, or the decoded JSON value is not a dict.
    """
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except binascii.Error as exc:
        raise ValueError(f"Invalid base64url input: {exc}") from exc

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid UTF-8 in payload: {exc}") from exc

    # json.JSONDecodeError is a subclass of ValueError, so it propagates as-is.
    result = json.loads(text)

    if not isinstance(result, dict):
        raise ValueError(
            f"Decoded JSON is not a dict, got {type(result).__name__!r}"
        )

    return result
