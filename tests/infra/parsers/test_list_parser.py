"""Tests for SelectolaxListParser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.errors import ParseBugError
from fis_monitor.domain.models import ParsedListRow
from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

from .conftest import load_fixture


@pytest.fixture()
def parser() -> SelectolaxListParser:
    return SelectolaxListParser()


@pytest.fixture()
def html_perpage50() -> str:
    return load_fixture("list_region1_perpage50.html")


@pytest.fixture()
def html_sorted_desc() -> str:
    return load_fixture("list_region1_sorted_desc_create.html")


# ---------------------------------------------------------------------------
# 1. Happy path -- list_region1_perpage50.html
# ---------------------------------------------------------------------------


def test_happy_path_returns_non_empty_list(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50)
    assert isinstance(rows, list)
    assert len(rows) > 0


def test_happy_path_first_row_has_required_fields(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50)
    first = rows[0]
    assert isinstance(first, ParsedListRow)
    assert isinstance(first.id, int)
    assert isinstance(first.cadastral_no, str)
    assert len(first.cadastral_no) > 0
    assert isinstance(first.region, str)
    assert len(first.region) > 0


def test_happy_path_first_row_known_values(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    """Verify first row matches known fixture data (id=1492)."""
    rows = parser.parse(html_perpage50)
    first = rows[0]
    assert first.id == 1492
    assert first.cadastral_no == "14:29:040004:523"
    assert first.region.startswith("Республика")
    assert first.municipality is not None
    assert len(first.municipality) > 0


# ---------------------------------------------------------------------------
# 2. Happy path sorted -- list_region1_sorted_desc_create.html
# ---------------------------------------------------------------------------


def test_sorted_desc_first_row_values(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    """Sorted fixture has id=9990 at top per sort-strategy.md verification."""
    rows = parser.parse(html_sorted_desc)
    assert len(rows) > 0
    first = rows[0]
    assert first.id == 9990
    assert first.cadastral_no == "79:06:2701002:287"


def test_sorted_desc_date_create_parses_correctly(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    rows = parser.parse(html_sorted_desc)
    first = rows[0]
    assert isinstance(first.date_create, datetime)
    # date_create for id=9990 is 12.05.2026
    assert first.date_create == datetime(2026, 5, 12, tzinfo=UTC)


def test_sorted_desc_top_rows_known_ids(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    """Top 3 rows per sort-strategy.md check (12.05.2026): 9990, 9989, 9988."""
    rows = parser.parse(html_sorted_desc)
    assert len(rows) >= 3
    assert rows[0].id == 9990
    assert rows[1].id == 9989
    assert rows[2].id == 9988


# ---------------------------------------------------------------------------
# 3. Field types
# ---------------------------------------------------------------------------


def test_field_types_are_correct(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50)
    for row in rows:
        assert isinstance(row.id, int)
        assert isinstance(row.cadastral_no, str)
        assert row.area_sqm is None or isinstance(row.area_sqm, int)
        assert isinstance(row.region, str)
        assert row.municipality is None or isinstance(row.municipality, str)
        assert row.land_category is None or isinstance(row.land_category, str)
        assert row.permitted_use is None or isinstance(row.permitted_use, str)
        assert row.ogv is None or isinstance(row.ogv, str)
        assert isinstance(row.status, str)
        assert isinstance(row.date_create, datetime)
        assert row.date_update is None or isinstance(row.date_update, datetime)


# ---------------------------------------------------------------------------
# 4. Empty/null fields -> None (not "" or 0)
# ---------------------------------------------------------------------------


def test_empty_fields_are_none_not_empty_string(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    """Sorted fixture id=9990 has empty permitted_use (data-col-seq=8 is empty)."""
    rows = parser.parse(html_sorted_desc)
    lot_9990 = next((r for r in rows if r.id == 9990), None)
    assert lot_9990 is not None
    # permitted_use is empty in fixture
    assert lot_9990.permitted_use is None, (
        f"Expected None for empty permitted_use, got {lot_9990.permitted_use!r}"
    )


def test_no_field_is_empty_string(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    """Parser invariant: no optional string field should be empty string."""
    rows = parser.parse(html_perpage50)
    for row in rows:
        assert row.municipality != "", (
            f"municipality must not be empty str in row {row.id}"
        )
        assert row.land_category != "", (
            f"land_category must not be empty str in row {row.id}"
        )
        assert row.permitted_use != "", (
            f"permitted_use must not be empty str in row {row.id}"
        )
        assert row.ogv != "", f"ogv must not be empty str in row {row.id}"


# ---------------------------------------------------------------------------
# 5. Empty list page -- parser returns [] without ParseBugError
# ---------------------------------------------------------------------------


def test_empty_tbody_returns_empty_list(parser: SelectolaxListParser) -> None:
    html = (
        "<html><body><table>"
        "<thead><tr><th>X</th></tr></thead>"
        "<tbody></tbody>"
        "</table></body></html>"
    )
    rows = parser.parse(html)
    assert rows == []


# ---------------------------------------------------------------------------
# 6. Malformed HTML -- no tbody -> ParseBugError
# ---------------------------------------------------------------------------


def test_missing_tbody_raises_parse_bug_error(parser: SelectolaxListParser) -> None:
    # HTML parsers auto-insert <tbody> for <table><tr> so use a page with no table.
    html = "<html><body><div>no table here</div></body></html>"
    with pytest.raises(ParseBugError):
        parser.parse(html)


def test_missing_table_raises_parse_bug_error(parser: SelectolaxListParser) -> None:
    html = "<html><body><p>No table here</p></body></html>"
    with pytest.raises(ParseBugError):
        parser.parse(html)


# ---------------------------------------------------------------------------
# 7. id parses as int (not str)
# ---------------------------------------------------------------------------


def test_id_is_int(parser: SelectolaxListParser, html_perpage50: str) -> None:
    rows = parser.parse(html_perpage50)
    for row in rows:
        assert type(row.id) is int, f"row.id should be int, got {type(row.id)}"


# ---------------------------------------------------------------------------
# 8. date_create is aware datetime (UTC)
# ---------------------------------------------------------------------------


def test_date_create_is_utc_aware(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50)
    for row in rows:
        assert row.date_create.tzinfo is not None, (
            f"date_create should be timezone-aware for row {row.id}"
        )
        assert row.date_create.tzinfo == UTC


# ---------------------------------------------------------------------------
# 9. area_sqm is integer
# ---------------------------------------------------------------------------


def test_area_sqm_is_integer(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50)
    rows_with_area = [r for r in rows if r.area_sqm is not None]
    assert len(rows_with_area) > 0, "Expected at least one row with area_sqm"
    for row in rows_with_area:
        assert isinstance(row.area_sqm, int), (
            f"area_sqm should be int for row {row.id}"
        )
        assert row.area_sqm > 0


def test_area_sqm_parsed_from_fixture_first_row(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    """First row has '3 998 kv.m' -> 3998."""
    rows = parser.parse(html_perpage50)
    first = rows[0]
    assert first.area_sqm == 3998


# ---------------------------------------------------------------------------
# 10. error_8_no_region.html -- redirect/error page -> ParseBugError
# ---------------------------------------------------------------------------


def test_error_page_no_region_raises_or_empty(parser: SelectolaxListParser) -> None:
    """error_8_no_region.html is a Gosuslugi redirect page (no tbody).
    Parser must raise ParseBugError (no tbody found)."""
    html = load_fixture("error_8_no_region.html")
    with pytest.raises(ParseBugError):
        parser.parse(html)


# ---------------------------------------------------------------------------
# 11. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows1 = parser.parse(html_perpage50)
    rows2 = parser.parse(html_perpage50)
    assert len(rows1) == len(rows2)
    for r1, r2 in zip(rows1, rows2, strict=True):
        assert r1.id == r2.id
        assert r1.cadastral_no == r2.cadastral_no
        assert r1.date_create == r2.date_create
        assert r1.area_sqm == r2.area_sqm
