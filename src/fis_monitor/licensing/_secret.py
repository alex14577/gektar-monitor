def _assemble_secret() -> bytes:
    """Assembles the HMAC secret at runtime from two XOR parts.

    Neither _P1 nor _P2 alone is the secret.
    strings(1) will not find the full secret in the binary.

    NOTE: The values of _P1 and _P2 below are PLACEHOLDERS only.
    Before first production use, run:
        python -m tools.gen_license init-secret
    and paste the printed literals into this function.
    """
    # PLACEHOLDER — replace with output of `init-secret` CLI before release.
    _P1: bytes = b"\xaa" * 32
    _P2: bytes = b"\x55" * 32
    return bytes(a ^ b for a, b in zip(_P1, _P2, strict=True))
