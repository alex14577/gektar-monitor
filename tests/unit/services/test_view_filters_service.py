"""Unit tests for ViewFilters Pydantic model + serialize/deserialize helpers.

Coverage:
  (a) Roundtrip: serialize(filters) → deserialize → identical ViewFilters.
  (b) deserialize with corrupted JSON → None.
  (c) deserialize with empty string → None.
  (d) deserialize with unknown keys is silently ignored (extra='ignore').
  (e) ViewFilters defaults are all falsy/empty (safe no-op).
  (f) area_min / area_max ge=0 constraints are enforced.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import pytest
from pydantic import ValidationError

from fis_monitor.services.view_filters import ViewFilters, deserialize, serialize

# ---------------------------------------------------------------------------
# (e) Defaults
# ---------------------------------------------------------------------------


def test_view_filters_defaults_are_empty() -> None:
    """Default ViewFilters represents «no filter applied»."""
    f = ViewFilters()
    assert f.subjects == []
    assert f.area_min is None
    assert f.area_max is None
    assert f.only_new is False


# ---------------------------------------------------------------------------
# (a) Roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_empty_filters_roundtrip(self) -> None:
        original = ViewFilters()
        recovered = deserialize(serialize(original))
        assert recovered is not None
        assert recovered == original

    def test_full_filters_roundtrip(self) -> None:
        original = ViewFilters(
            subjects=["Московская область", "Краснодарский край"],
            area_min=10,
            area_max=500,
            only_new=True,
        )
        recovered = deserialize(serialize(original))
        assert recovered is not None
        assert recovered.subjects == original.subjects
        assert recovered.area_min == original.area_min
        assert recovered.area_max == original.area_max
        assert recovered.only_new == original.only_new

    def test_none_area_values_survive_roundtrip(self) -> None:
        original = ViewFilters(area_min=None, area_max=None)
        recovered = deserialize(serialize(original))
        assert recovered is not None
        assert recovered.area_min is None
        assert recovered.area_max is None

    def test_serialize_produces_percent_encoded_valid_json(self) -> None:
        """serialize() percent-encodes JSON so non-ASCII chars are cookie-safe."""
        f = ViewFilters(subjects=["Свердловская область"], only_new=True)
        raw = serialize(f)
        # The raw cookie value must be ASCII-only (latin-1 safe)
        raw.encode("latin-1")  # raises UnicodeEncodeError if not ASCII-safe
        # Decoded value must be valid JSON with correct content
        parsed = json.loads(unquote(raw))
        assert parsed["subjects"] == ["Свердловская область"]
        assert parsed["only_new"] is True


# ---------------------------------------------------------------------------
# (b) Corrupted JSON
# ---------------------------------------------------------------------------


class TestDeserialiseErrors:
    def test_not_json_returns_none(self) -> None:
        assert deserialize("not-json!!!") is None

    def test_empty_string_returns_none(self) -> None:
        assert deserialize("") is None

    def test_none_like_empty_returns_none(self) -> None:
        # None cannot be passed (type is str), but whitespace shouldn't crash
        assert deserialize("   ") is None

    def test_json_array_returns_none(self) -> None:
        """A JSON array is not a valid ViewFilters dict — should return None."""
        from urllib.parse import quote

        assert deserialize(quote("[1,2,3]", safe="")) is None


# ---------------------------------------------------------------------------
# (d) Unknown keys are ignored
# ---------------------------------------------------------------------------


def test_unknown_keys_in_cookie_are_ignored() -> None:
    """Forward-compat: extra keys from future schema versions are silently dropped."""
    from urllib.parse import quote

    raw_json = json.dumps({"subjects": [], "future_key": "ignored"})
    # Cookie value is percent-encoded JSON
    encoded = quote(raw_json, safe="")
    result = deserialize(encoded)
    assert result is not None
    assert result.subjects == []
    # If we get here without ValidationError, extra='ignore' worked correctly


# ---------------------------------------------------------------------------
# Regression: old cookie with sort_dir field must not crash (ewqq)
# ---------------------------------------------------------------------------


def test_deserialize_old_cookie_with_sort_dir_ignores_field() -> None:
    """Decoding a cookie that still contains sort_dir must not raise; field is silently ignored."""
    import json
    from urllib.parse import quote

    old_json = json.dumps(
        {"subjects": [], "area_min": None, "area_max": None, "only_new": False, "sort_dir": "asc"}
    )
    encoded = quote(old_json, safe="")
    result = deserialize(encoded)
    assert result is not None


# ---------------------------------------------------------------------------
# (f) Field constraints
# ---------------------------------------------------------------------------


class TestConstraints:
    def test_area_min_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            ViewFilters(area_min=-1)

    def test_area_max_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            ViewFilters(area_max=-100)

    def test_area_min_zero_ok(self) -> None:
        f = ViewFilters(area_min=0)
        assert f.area_min == 0

    def test_subjects_is_list(self) -> None:
        f = ViewFilters(subjects=["subject_one", "subject_two"])
        assert len(f.subjects) == 2
