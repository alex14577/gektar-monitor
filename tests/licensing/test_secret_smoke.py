from fis_monitor.licensing._secret import _assemble_secret


def test_assemble_secret_returns_bytes_of_correct_length() -> None:
    secret = _assemble_secret()
    assert isinstance(secret, bytes)
    assert len(secret) == 32
    assert secret != b"\x00" * 32  # guard against degenerate _P1 == _P2
