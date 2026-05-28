def _assemble_secret() -> bytes:
    """Assembles the HMAC secret at runtime from two XOR parts.

    Neither _P1 nor _P2 alone is the secret.
    strings(1) will not find the full secret in the binary.

    NOTE: The values of _P1 and _P2 below are PLACEHOLDERS only.
    Before first production use, run:
        python -m tools.gen_license init-secret
    and paste the printed literals into this function.
    """
    # Secret initialized via `python -m tools.gen_license init-secret`.
    # Do NOT re-run init-secret — it would rotate the secret and invalidate
    # all existing license keys.
    _P1: bytes = (
        b'U\xd3\x94\x8d\xe2\x90\xba\xcfL\x88\xd4Y\x8d\x96\x95{'
        b'@\x1e\xf5\xf0\xd9\xd4\xa2\x8a\xe57?\xa583\x8c\x9c'
    )
    _P2: bytes = (
        b'\x0c \x1aa(\x82\x98E\xa0c\x88H2Z\xdccAn\x84\xc7Q\xf2'
        b'\xfe4\xd3<8\xb8\x9c\x01V{'
    )
    return bytes(a ^ b for a, b in zip(_P1, _P2, strict=True))
