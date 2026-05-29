"""Parametrized tests for verify_license — v2 payload contract.

11 invariant cases covering validity window, HMAC integrity, version dispatch,
and determinism smoke.
"""

import base64
import json
from datetime import UTC, date, datetime

import pytest

from fis_monitor.licensing._verify import LicenseStatus, verify_license
from tests.licensing.conftest import _TEST_SECRET, make_v2_key

TODAY = date(2026, 1, 15)
NBF = date(2026, 1, 1)
EXP = date(2026, 6, 30)


def _dt(d: date) -> datetime:
    """Wrap a date as a noon-UTC datetime for injection into verify_license."""
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=UTC)


def _tamper_segment(key: str, segment_index: int) -> str:
    """Flip the first character in the specified dot-separated segment.

    ``key`` format: ``v2.<payload>.<sig>``
    segment_index 0 → payload, 1 → sig.
    """
    parts = key.split(".")
    target_idx = segment_index + 1
    segment = parts[target_idx]
    if segment[0] == "A":
        parts[target_idx] = "B" + segment[1:]
    else:
        parts[target_idx] = "A" + segment[1:]
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Pre-built keys
# ---------------------------------------------------------------------------

_VALID_KEY = make_v2_key(NBF, EXP, _TEST_SECRET)
_BOUNDARY_EXP_KEY = make_v2_key(NBF, TODAY, _TEST_SECRET)      # today == exp
_BOUNDARY_NBF_KEY = make_v2_key(TODAY, EXP, _TEST_SECRET)      # today == nbf
_EXPIRED_KEY = make_v2_key(NBF, date(2026, 1, 14), _TEST_SECRET)  # exp yesterday
_FUTURE_NBF_KEY = make_v2_key(date(2026, 1, 16), EXP, _TEST_SECRET)  # nbf tomorrow

_TAMPERED_PAYLOAD_KEY = _tamper_segment(_VALID_KEY, 0)
_TAMPERED_SIG_KEY = _tamper_segment(_VALID_KEY, 1)
_MALFORMED_KEY = "v2.!!!not-base64!!!"
_UNKNOWN_VERSION_KEY = "v3.foo.bar"

# Build a v1-style key manually (simulate old format, must be rejected)
_V1_LITERAL_KEY = "v1.eyJ2IjoxLCJpYXQiOiIyMDI2LTAxLTAxIiwibGljIjoiYWNtZSJ9.AAAA"


@pytest.mark.parametrize(
    "key_str, now_date, expected_status, expected_exp",
    [
        # 1. VALID: today within [nbf, exp]
        pytest.param(
            _VALID_KEY, TODAY, LicenseStatus.VALID, EXP,
            id="valid_within_range",
        ),
        # 2. VALID: today == nbf (inclusive lower boundary)
        pytest.param(
            _BOUNDARY_NBF_KEY, TODAY, LicenseStatus.VALID, EXP,
            id="valid_boundary_nbf_equals_today",
        ),
        # 3. VALID: today == exp (inclusive upper boundary)
        pytest.param(
            _BOUNDARY_EXP_KEY, TODAY, LicenseStatus.VALID, TODAY,
            id="valid_boundary_exp_equals_today",
        ),
        # 4. INVALID: today < nbf (anti-rollback)
        pytest.param(
            _FUTURE_NBF_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_before_nbf",
        ),
        # 5. EXPIRED: today > exp
        pytest.param(
            _EXPIRED_KEY, TODAY, LicenseStatus.EXPIRED, date(2026, 1, 14),
            id="expired_past_exp",
        ),
        # 6. INVALID: tampered payload (HMAC mismatch)
        pytest.param(
            _TAMPERED_PAYLOAD_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_tampered_payload",
        ),
        # 7. INVALID: tampered signature
        pytest.param(
            _TAMPERED_SIG_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_tampered_sig",
        ),
        # 8. INVALID: malformed base64 in payload
        pytest.param(
            _MALFORMED_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_malformed_base64",
        ),
        # 9. INVALID: unknown version prefix
        pytest.param(
            _UNKNOWN_VERSION_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_unknown_version",
        ),
        # 10. INVALID: v1 literal → unsupported version
        pytest.param(
            _V1_LITERAL_KEY, TODAY, LicenseStatus.INVALID, None,
            id="invalid_v1_unsupported_version",
        ),
    ],
)
def test_verify_license(
    key_str: str,
    now_date: date,
    expected_status: LicenseStatus,
    expected_exp: date | None,
) -> None:
    """Each of the 10 invariant cases from the v2 licensing spec."""
    result = verify_license(key_str, _TEST_SECRET, _dt(now_date))
    assert result.status == expected_status
    if expected_exp is not None:
        assert result.expires_at == expected_exp
    else:
        assert result.expires_at is None


def test_verify_license_is_deterministic() -> None:
    """Two identical calls with same (key, secret, now) return equal results.

    Verifies the DI invariant: no hidden datetime.now() inside verify_license.
    """
    key = make_v2_key(NBF, EXP, _TEST_SECRET)
    now = _dt(TODAY)

    result_a = verify_license(key, _TEST_SECRET, now)
    result_b = verify_license(key, _TEST_SECRET, now)
    assert result_a == result_b
    assert result_a.status == LicenseStatus.VALID


def test_make_v2_key_encodes_v2_payload_fields() -> None:
    """make_v2_key produces a key whose payload contains v==2, nbf, exp, lic.

    Verifies the payload structure contract by decoding the key directly.
    """
    key = make_v2_key(NBF, EXP, _TEST_SECRET)
    parts = key.split(".")
    assert parts[0] == "v2"
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded))
    assert decoded["v"] == 2
    assert decoded["nbf"] == NBF.isoformat()
    assert decoded["exp"] == EXP.isoformat()
    assert decoded["lic"] == "interactive"


def test_verify_license_naive_datetime_returns_invalid() -> None:
    """verify_license with a naive datetime (no tzinfo) returns INVALID.

    SE-3: naive datetime guard must reject the call before any key parsing.
    """
    from datetime import datetime

    key = make_v2_key(NBF, EXP, _TEST_SECRET)
    naive_now = datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, 0)  # no tzinfo
    result = verify_license(key, _TEST_SECRET, naive_now)
    assert result.status == LicenseStatus.INVALID
    assert result.expires_at is None


def _make_key_with_bad_payload_types(
    nbf: date,
    exp: date,
    secret: bytes,
    *,
    nbf_value: object,
    exp_value: object,
) -> str:
    """Build a v2 key where nbf/exp are replaced with non-string values.

    The HMAC is computed over the modified payload (so signature is valid),
    but _extract_v2_fields should still reject the bad types.
    """
    from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
    from fis_monitor.licensing._hmac import sign

    payload: dict[str, object] = {
        "v": 2,
        "nbf": nbf_value,
        "exp": exp_value,
        "lic": "interactive",
    }
    encoded_payload = encode_payload(payload)
    sig = sign(_canonical_bytes(payload), secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"v2.{encoded_payload}.{encoded_sig}"


@pytest.mark.parametrize(
    "nbf_value, exp_value, label",
    [
        (20260101, EXP.isoformat(), "nbf_is_int"),
        (None, EXP.isoformat(), "nbf_is_null"),
        (NBF.isoformat(), [], "exp_is_list"),
    ],
)
def test_verify_license_invalid_payload_field_types(
    nbf_value: object,
    exp_value: object,
    label: str,
) -> None:
    """Payload with wrong field types but valid HMAC → INVALID.

    SE-MINOR-6: _extract_v2_fields rejects non-string nbf/exp even when
    the HMAC is correct (key was signed with those bad values).
    """
    key = _make_key_with_bad_payload_types(
        NBF, EXP, _TEST_SECRET, nbf_value=nbf_value, exp_value=exp_value
    )
    result = verify_license(key, _TEST_SECRET, _dt(TODAY))
    assert result.status == LicenseStatus.INVALID, f"Expected INVALID for case {label}"
    assert result.expires_at is None
