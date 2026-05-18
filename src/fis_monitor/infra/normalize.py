"""Normalisation helpers for parsed lot fields.

All functions are pure — no I/O, no side effects.
Used by ``infra/parsers/list_parser.py`` before constructing ``ParsedListRow``.
"""
from __future__ import annotations

# Whitelist of abbreviation-form VRI values that must stay uppercase.
# .capitalize() would produce "Ижс", "Лпх", etc. — wrong for abbreviations.
# Adding a new abbreviation: extend the frozenset here; no other change needed.
_ABBREVIATIONS: frozenset[str] = frozenset({"ИЖС", "ЛПХ", "СНТ", "ДНТ", "ОНТ", "КФХ"})


def normalize_vri(value: str | None) -> str | None:
    """Normalise a «Вид разрешённого использования» string from the parser.

    Rules (in priority order):
    1. ``None`` or empty → return as-is (no mutation).
    2. If ``value.strip().upper()`` is in the abbreviation whitelist → return
       the uppercased form.  Handles «лпх», «Лпх», «ЛПХ» → «ЛПХ».
    3. Otherwise capitalise only the first character (``s[0].upper() + s[1:]``).
       This preserves internal capitalisation — unlike ``str.capitalize()``
       which lower-cases everything after the first char.

    Examples:
        normalize_vri("ижс") → "ИЖС"
        normalize_vri("ЛПХ") → "ЛПХ"
        normalize_vri("для ведения ЛПХ") → "Для ведения ЛПХ"
        normalize_vri("ведение садоводства") → "Ведение садоводства"
        normalize_vri("") → ""
        normalize_vri(None) → None

    Args:
        value: Raw string from the parser or ``None``.

    Returns:
        Normalised string or the original value when no transformation applies.
    """
    if not value:
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped.upper() in _ABBREVIATIONS:
        return stripped.upper()
    # First-char capitalise — preserves the rest as-is
    return stripped[0].upper() + stripped[1:]
