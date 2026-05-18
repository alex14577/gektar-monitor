"""Unit tests for ``infra.normalize.normalize_vri``.

Layer 3 (Infra) — pure function, no infrastructure.
Covers invariants from spec: None, empty, abbreviations (case-insensitive),
first-char capitalise, internal uppercase preserved.
"""
from __future__ import annotations

import pytest

from fis_monitor.infra.normalize import normalize_vri


@pytest.mark.parametrize("value, expected", [
    # None and empty passthrough
    (None, None),
    ("", ""),
    # Abbreviation: exact uppercase form
    ("ИЖС", "ИЖС"),
    ("ЛПХ", "ЛПХ"),
    ("СНТ", "СНТ"),
    ("ДНТ", "ДНТ"),
    ("ОНТ", "ОНТ"),
    ("КФХ", "КФХ"),
    # Abbreviation: case-insensitive trigger
    ("ижс", "ИЖС"),
    ("лпх", "ЛПХ"),
    ("Лпх", "ЛПХ"),
    # NOT an abbreviation: first-char capitalise only
    ("для ведения ЛПХ", "Для ведения ЛПХ"),
    ("ведение садоводства", "Ведение садоводства"),
    ("ведение огородничества", "Ведение огородничества"),
    # Already correctly capitalised
    ("Ведение садоводства", "Ведение садоводства"),
    # Internal uppercase preserved (not .capitalize()-lowercased)
    ("для ИНДИВИДУАЛЬНОГО жилого строительства", "Для ИНДИВИДУАЛЬНОГО жилого строительства"),
])
def test_normalize_vri(value: str | None, expected: str | None) -> None:
    assert normalize_vri(value) == expected


def test_whitespace_stripped_before_abbreviation_check() -> None:
    """Leading/trailing whitespace stripped before checking abbreviation."""
    assert normalize_vri("  ЛПХ  ") == "ЛПХ"
    assert normalize_vri("  лпх  ") == "ЛПХ"
