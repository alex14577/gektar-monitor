"""Parametrized tests for verify_license — §11 of the licensing spec.

10 invariant cases + 1 determinism smoke test.
"""

from datetime import UTC, date, datetime

import pytest

from fis_monitor.licensing._verify import LicenseStatus, verify_license
from tests.licensing.conftest import _TEST_SECRET, make_key

TODAY = date(2026, 1, 15)
IAT = date(2026, 1, 1)
EXP = date(2026, 6, 30)


def _dt(d: date) -> datetime:
    """Wrap a date as a noon-UTC datetime for injection into verify_license."""
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=UTC)


def _tamper_segment(key: str, segment_index: int) -> str:
    """Flip one character in the specified dot-separated segment (0-based after prefix).

    ``key`` format: ``v1.<payload>.<sig>``
    segment_index 0 → payload, 1 → sig.
    """
    parts = key.split(".")
    # parts[0] = "v1", parts[1] = payload, parts[2] = sig
    target_idx = segment_index + 1
    segment = parts[target_idx]
    # Replace first 'A' with 'B' or first char with next char
    if segment[0] == "A":
        parts[target_idx] = "B" + segment[1:]
    else:
        parts[target_idx] = "A" + segment[1:]
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Parametrized cases
# ---------------------------------------------------------------------------

_VALID_KEY = make_key("acme", IAT, EXP, _TEST_SECRET)
_PERPETUAL_KEY = make_key("acme", IAT, None, _TEST_SECRET)
_BOUNDARY_EXP_KEY = make_key("acme", IAT, TODAY, _TEST_SECRET)      # today == exp
_BOUNDARY_IAT_KEY = make_key("acme", TODAY, EXP, _TEST_SECRET)       # today == iat
_EXPIRED_KEY = make_key("acme", IAT, date(2026, 1, 14), _TEST_SECRET)  # exp yesterday
_FUTURE_IAT_KEY = make_key("acme", date(2026, 1, 16), EXP, _TEST_SECRET)  # iat tomorrow

_TAMPERED_PAYLOAD_KEY = _tamper_segment(_VALID_KEY, 0)
_TAMPERED_SIG_KEY = _tamper_segment(_VALID_KEY, 1)
_MALFORMED_KEY = "v1.!!!not-base64!!!"
_UNKNOWN_VERSION_KEY = "v9.foo.bar"


@pytest.mark.parametrize(
    "key_str, now_date, expected_status, expected_exp",
    [
        # 1. VALID: today between iat and exp
        pytest.param(
            _VALID_KEY, TODAY, LicenseStatus.VALID, EXP,
            id="valid_within_range",
        ),
        # 2. VALID: perpetual (exp=None)
        pytest.param(
            _PERPETUAL_KEY, TODAY, LicenseStatus.VALID, None,
            id="valid_perpetual",
        ),
        # 3. VALID: today == exp_date (inclusive boundary)
        pytest.param(
            _BOUNDARY_EXP_KEY, TODAY, LicenseStatus.VALID, TODAY,
            id="valid_boundary_exp_equals_today",
        ),
        # 4. VALID: today == iat_date (inclusive boundary)
        pytest.param(
            _BOUNDARY_IAT_KEY, TODAY, LicenseStatus.VALID, EXP,
            id="valid_boundary_iat_equals_today",
        ),
        # 5. EXPIRED: today > exp_date
        pytest.param(
            _EXPIRED_KEY, TODAY, LicenseStatus.EXPIRED, date(2026, 1, 14),
            id="expired_past_exp",
        ),
        # 6. INVALID: today < iat_date (rollback protection)
        pytest.param(
            _FUTURE_IAT_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_before_iat",
        ),
        # 7. INVALID: tampered payload (HMAC mismatch)
        pytest.param(
            _TAMPERED_PAYLOAD_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_tampered_payload",
        ),
        # 8. INVALID: tampered signature
        pytest.param(
            _TAMPERED_SIG_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_tampered_sig",
        ),
        # 9. INVALID: malformed base64
        pytest.param(
            _MALFORMED_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_malformed_base64",
        ),
        # 10. INVALID: unknown version prefix
        pytest.param(
            _UNKNOWN_VERSION_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_unknown_version",
        ),
    ],
)
def test_verify_license(
    key_str: str,
    now_date: date,
    expected_status: LicenseStatus,
    expected_exp: date | None,
) -> None:
    """Each of the 10 invariant cases from §11 of the licensing spec."""
    result = verify_license(key_str, _TEST_SECRET, _dt(now_date))
    assert result.status == expected_status
    if expected_exp is not None:
        assert result.expires_at == expected_exp
    else:
        assert result.expires_at is None


def test_verify_license_is_deterministic() -> None:
    """Two calls with identical (key, secret, now) must return equal results. DI invariant."""
    key = make_key("determinism-check", IAT, EXP, _TEST_SECRET)
    now = _dt(TODAY)
    result_a = verify_license(key, _TEST_SECRET, now)
    result_b = verify_license(key, _TEST_SECRET, now)
    assert result_a == result_b
    assert result_a.status == LicenseStatus.VALID
