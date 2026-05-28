"""HMAC-SHA256 signing and constant-time verification for license payloads."""

import hashlib
import hmac


def sign(payload_bytes: bytes, secret: bytes) -> bytes:
    """Compute HMAC-SHA256 signature over payload_bytes.

    Args:
        payload_bytes: Raw bytes to sign.
        secret: HMAC secret key bytes.

    Returns:
        32-byte HMAC-SHA256 digest.
    """
    return hmac.new(key=secret, msg=payload_bytes, digestmod=hashlib.sha256).digest()


def verify_signature(payload_bytes: bytes, sig_bytes: bytes, secret: bytes) -> bool:
    """Verify HMAC-SHA256 signature in constant time.

    Args:
        payload_bytes: Raw bytes that were signed.
        sig_bytes: Signature bytes to verify against.
        secret: HMAC secret key bytes.

    Returns:
        True if the signature matches, False otherwise.
    """
    expected = sign(payload_bytes, secret)
    return hmac.compare_digest(expected, sig_bytes)
