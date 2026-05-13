"""Tests for domain/diff.py — compute_changes pure function.

TDD: these tests are written BEFORE the implementation (red → green).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fis_monitor.domain.diff import compute_changes
from fis_monitor.domain.models import FieldChange


# ---------------------------------------------------------------------------
# 1. old=None → always returns []
# ---------------------------------------------------------------------------
def test_old_none_returns_empty(make_lot):
    new = make_lot()
    result = compute_changes(None, new, ["status", "area_sqm", "is_active"])
    assert result == []


def test_old_none_empty_tracked_returns_empty(make_lot):
    new = make_lot()
    assert compute_changes(None, new, []) == []


# ---------------------------------------------------------------------------
# 2. Identical old/new → []
# ---------------------------------------------------------------------------
def test_identical_lots_no_changes(make_lot):
    lot = make_lot()
    result = compute_changes(lot, lot, ["status", "area_sqm", "date_update", "is_active"])
    assert result == []


# ---------------------------------------------------------------------------
# 3. Single field change: status
# ---------------------------------------------------------------------------
def test_single_status_change(make_lot):
    old = make_lot(status="Свободен")
    new = make_lot(status="Зарезервирован")
    result = compute_changes(old, new, ["status"])
    assert len(result) == 1
    change = result[0]
    assert isinstance(change, FieldChange)
    assert change.field == "status"
    assert change.old_value == "Свободен"
    assert change.new_value == "Зарезервирован"


# ---------------------------------------------------------------------------
# 4. Multiple field changes — order matches tracked order
# ---------------------------------------------------------------------------
def test_multiple_changes_order_matches_tracked(make_lot):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    later = datetime(2026, 5, 13, 13, 0, 0, tzinfo=UTC)
    old = make_lot(status="Свободен", area_sqm=10_000, date_update=now)
    new = make_lot(status="Зарезервирован", area_sqm=5_000, date_update=later)

    tracked = ["date_update", "area_sqm", "status"]  # deliberate non-alphabetical
    result = compute_changes(old, new, tracked)

    assert len(result) == 3
    assert [c.field for c in result] == ["date_update", "area_sqm", "status"]


# ---------------------------------------------------------------------------
# 5. NULL → value (area_sqm None → 5000)
# ---------------------------------------------------------------------------
def test_null_to_value_area_sqm(make_lot):
    old = make_lot(area_sqm=None)
    new = make_lot(area_sqm=5_000)
    result = compute_changes(old, new, ["area_sqm"])
    assert len(result) == 1
    assert result[0].old_value is None
    assert result[0].new_value == 5_000


# ---------------------------------------------------------------------------
# 6. value → NULL (area_sqm 5000 → None)
# ---------------------------------------------------------------------------
def test_value_to_null_area_sqm(make_lot):
    old = make_lot(area_sqm=5_000)
    new = make_lot(area_sqm=None)
    result = compute_changes(old, new, ["area_sqm"])
    assert len(result) == 1
    assert result[0].old_value == 5_000
    assert result[0].new_value is None


# ---------------------------------------------------------------------------
# 7. date_update change (datetime field)
# ---------------------------------------------------------------------------
def test_date_update_change(make_lot):
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 5, 13, tzinfo=UTC)
    old = make_lot(date_update=t1)
    new = make_lot(date_update=t2)
    result = compute_changes(old, new, ["date_update"])
    assert len(result) == 1
    assert result[0].old_value == t1
    assert result[0].new_value == t2


# ---------------------------------------------------------------------------
# 8. is_active True → False
# ---------------------------------------------------------------------------
def test_is_active_change(make_lot):
    old = make_lot(is_active=True)
    new = make_lot(is_active=False)
    result = compute_changes(old, new, ["is_active"])
    assert len(result) == 1
    assert result[0].old_value is True
    assert result[0].new_value is False


# ---------------------------------------------------------------------------
# 9. Field NOT in tracked is ignored even if it differs
# ---------------------------------------------------------------------------
def test_untracked_field_ignored(make_lot):
    old = make_lot(region="Хабаровский край", status="Свободен")
    new = make_lot(region="Приморский край", status="Свободен")
    # region differs, but we only track status
    result = compute_changes(old, new, ["status"])
    assert result == []


# ---------------------------------------------------------------------------
# 10. Unknown field in tracked → ValueError mentioning the field name
# ---------------------------------------------------------------------------
def test_unknown_field_raises_value_error(make_lot):
    lot = make_lot()
    with pytest.raises(ValueError, match="foo"):
        compute_changes(lot, lot, ["foo"])  # type: ignore[list-item]


def test_empty_tracked_with_real_old_returns_empty(make_lot):
    """Pin the no-tracked-fields path explicitly (old is not None)."""
    old = make_lot(status="Свободен")
    new = make_lot(status="Зарезервирован")
    assert compute_changes(old, new, []) == []


@pytest.mark.parametrize("field", ["auction", "list_presence"])
def test_forward_compat_fields_raise_not_implemented(make_lot, field):
    """auction/list_presence are reserved in TrackedField but not on Lot yet.

    Must fail fast with NotImplementedError before any getattr — protects
    callers that run inside a BEGIN IMMEDIATE tx from leaking AttributeError.
    """
    lot = make_lot()
    with pytest.raises(NotImplementedError, match=field):
        compute_changes(lot, lot, [field])


def test_allowed_tracked_fields_matches_literal():
    """ALLOWED_TRACKED_FIELDS is SSOT-derived from the TrackedField Literal."""
    import typing as _typing

    from fis_monitor.domain.diff import ALLOWED_TRACKED_FIELDS
    from fis_monitor.domain.models import TrackedField

    assert frozenset(_typing.get_args(TrackedField)) == ALLOWED_TRACKED_FIELDS


# ---------------------------------------------------------------------------
# 11. Idempotence / symmetry (property-based if hypothesis available)
# ---------------------------------------------------------------------------
def test_idempotence_same_lot(make_lot):
    """compute_changes(a, a, tracked) == [] for any lot."""
    lot = make_lot()
    tracked = ["status", "area_sqm", "date_update", "is_active"]
    assert compute_changes(lot, lot, tracked) == []


def test_symmetry_in_length(make_lot):
    """len(compute_changes(a, b, t)) == len(compute_changes(b, a, t))."""
    a = make_lot(status="Свободен", area_sqm=1_000)
    b = make_lot(status="Зарезервирован", area_sqm=2_000)
    tracked = ["status", "area_sqm", "is_active"]
    assert len(compute_changes(a, b, tracked)) == len(compute_changes(b, a, tracked))


def test_property_based_with_hypothesis(make_lot):
    """Hypothesis-based idempotence and symmetry, skipped if not installed."""
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    statuses = ["Свободен", "Зарезервирован", "Продан"]
    tracked = ["status", "area_sqm", "is_active"]

    @given(
        s1=st.sampled_from(statuses),
        s2=st.sampled_from(statuses),
        a1=st.one_of(st.none(), st.integers(min_value=100, max_value=100_000)),
        a2=st.one_of(st.none(), st.integers(min_value=100, max_value=100_000)),
        active1=st.booleans(),
        active2=st.booleans(),
    )
    @settings(max_examples=50)
    def _inner(s1, s2, a1, a2, active1, active2):
        lot_a = make_lot(status=s1, area_sqm=a1, is_active=active1)
        lot_b = make_lot(status=s2, area_sqm=a2, is_active=active2)
        # idempotence
        assert compute_changes(lot_a, lot_a, tracked) == []
        # symmetry of length
        assert len(compute_changes(lot_a, lot_b, tracked)) == len(
            compute_changes(lot_b, lot_a, tracked)
        )

    _inner()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 12. Result elements are FieldChange instances and frozen
# ---------------------------------------------------------------------------
def test_result_elements_are_field_change_instances(make_lot):
    old = make_lot(status="Свободен")
    new = make_lot(status="Зарезервирован")
    result = compute_changes(old, new, ["status"])
    assert all(isinstance(c, FieldChange) for c in result)


def test_result_elements_are_frozen(make_lot):
    old = make_lot(status="Свободен")
    new = make_lot(status="Зарезервирован")
    result = compute_changes(old, new, ["status"])
    assert len(result) == 1
    with pytest.raises((ValidationError, TypeError)):
        result[0].field = "area_sqm"  # type: ignore[misc]
