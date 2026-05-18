"""Unit tests for ``LotFilters`` sort_dir invariant (hiq3 spec, TESTS section).

Placed in tests.unit.web because ``lot_query`` transitively imports sqlite3
(via ``infra.sqlite.repositories.lots.row_to_lot``), which the import-linter
forbids in ``tests.unit.services`` (ADR-041 §Layer location rule).

Covered invariants:
- Default sort_dir is "desc".
- sort_dir="asc" is accepted.
- Invalid sort_dir raises ValueError in __post_init__.
"""

from __future__ import annotations

import pytest

from fis_monitor.services.lot_query import LotFilters


class TestLotFiltersSortDir:
    """sort_dir validation invariants for LotFilters."""

    def test_default_sort_dir_is_desc(self) -> None:
        """LotFilters sort_dir defaults to 'desc' (newest first)."""
        f = LotFilters()
        assert f.sort_dir == "desc"

    def test_sort_dir_asc_accepted(self) -> None:
        """LotFilters accepts sort_dir='asc' without error."""
        f = LotFilters(sort_dir="asc")
        assert f.sort_dir == "asc"

    def test_invalid_sort_dir_raises_value_error(self) -> None:
        """LotFilters.__post_init__ rejects any value that is not 'desc' or 'asc'."""
        with pytest.raises(ValueError, match="sort_dir must be 'desc' or 'asc'"):
            LotFilters(sort_dir="random")  # type: ignore[arg-type]
