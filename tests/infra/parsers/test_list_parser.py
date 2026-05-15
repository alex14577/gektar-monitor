"""Tests for SelectolaxListParser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.errors import ParseBugError, SessionExpiredError
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
    rows = parser.parse(html_perpage50).rows
    assert isinstance(rows, list)
    assert len(rows) > 0


def test_happy_path_first_row_has_required_fields(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50).rows
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
    rows = parser.parse(html_perpage50).rows
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
    rows = parser.parse(html_sorted_desc).rows
    assert len(rows) > 0
    first = rows[0]
    assert first.id == 9990
    assert first.cadastral_no == "79:06:2701002:287"


def test_sorted_desc_date_create_parses_correctly(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    rows = parser.parse(html_sorted_desc).rows
    first = rows[0]
    assert isinstance(first.date_create, datetime)
    # date_create for id=9990 is 12.05.2026
    assert first.date_create == datetime(2026, 5, 12, tzinfo=UTC)


def test_sorted_desc_top_rows_known_ids(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    """Top 3 rows per sort-strategy.md check (12.05.2026): 9990, 9989, 9988."""
    rows = parser.parse(html_sorted_desc).rows
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
    rows = parser.parse(html_perpage50).rows
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
    rows = parser.parse(html_sorted_desc).rows
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
    rows = parser.parse(html_perpage50).rows
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
    page = parser.parse(html)
    assert page.rows == []


# ---------------------------------------------------------------------------
# 6. Malformed HTML -- no tbody -> ParseBugError
# ---------------------------------------------------------------------------


def test_missing_tbody_raises_parse_bug_error(parser: SelectolaxListParser) -> None:
    # HTML parsers auto-insert <tbody> for <table><tr> so use a page with no table.
    html = "<html><body><div>no table here</div></body></html>"
    with pytest.raises(ParseBugError) as exc_info:
        parser.parse(html)
    assert exc_info.value.selector == "tbody"


def test_missing_table_raises_parse_bug_error(parser: SelectolaxListParser) -> None:
    html = "<html><body><p>No table here</p></body></html>"
    with pytest.raises(ParseBugError) as exc_info:
        parser.parse(html)
    assert exc_info.value.selector == "tbody"


# ---------------------------------------------------------------------------
# 7. id parses as int (not str)
# ---------------------------------------------------------------------------


def test_id_is_int(parser: SelectolaxListParser, html_perpage50: str) -> None:
    rows = parser.parse(html_perpage50).rows
    for row in rows:
        assert type(row.id) is int, f"row.id should be int, got {type(row.id)}"


# ---------------------------------------------------------------------------
# 8. date_create is aware datetime (UTC)
# ---------------------------------------------------------------------------


def test_date_create_is_utc_aware(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows = parser.parse(html_perpage50).rows
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
    rows = parser.parse(html_perpage50).rows
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
    rows = parser.parse(html_perpage50).rows
    first = rows[0]
    assert first.area_sqm == 3998


# ---------------------------------------------------------------------------
# 10. error_8_no_region.html -- redirect/error page -> SessionExpiredError
# ---------------------------------------------------------------------------


def test_error_page_no_region_raises_session_expired(parser: SelectolaxListParser) -> None:
    """error_8_no_region.html is a Gosuslugi redirect page (title 'Портал государственных услуг').
    Parser must raise SessionExpiredError — this is an auth failure, not a DOM bug."""
    html = load_fixture("error_8_no_region.html")
    with pytest.raises(SessionExpiredError):
        parser.parse(html)


# ---------------------------------------------------------------------------
# 11. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    rows1 = parser.parse(html_perpage50).rows
    rows2 = parser.parse(html_perpage50).rows
    assert len(rows1) == len(rows2)
    for r1, r2 in zip(rows1, rows2, strict=True):
        assert r1.id == r2.id
        assert r1.cadastral_no == r2.cadastral_no
        assert r1.date_create == r2.date_create
        assert r1.area_sqm == r2.area_sqm


# ---------------------------------------------------------------------------
# 12. ESIA / SessionExpiredError detection
# ---------------------------------------------------------------------------

# ESIA redirect page: title contains "Портал государственных услуг"
_ESIA_HOST_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Портал государственных услуг</title></head>
<body>
  <p>Для продолжения войдите через esia.gosuslugi.ru</p>
</body>
</html>
"""

# ESIA redirect page: title contains "esia.gosuslugi.ru" (hypothetical)
_ESIA_TITLE_HOST_HTML = """\
<!DOCTYPE html>
<html>
<head><title>esia.gosuslugi.ru — войти</title></head>
<body>
  <p>Введите логин и пароль</p>
</body>
</html>
"""

_GOSUSLUGI_TITLE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Госуслуги — вход</title></head>
<body><p>Войдите в систему</p></body>
</html>
"""

# Normal lot-list page that happens to have esia.gosuslugi.ru in a nav link —
# must NOT trigger SessionExpiredError.
_NORMAL_PAGE_WITH_ESIA_NAVLINK_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Список свободных лотов</title></head>
<body>
  <nav>
    <a href="https://esia.gosuslugi.ru/registration">Зарегистрироваться</a>
  </nav>
  <table>
    <tbody>
    </tbody>
  </table>
</body>
</html>
"""

_NORMAL_EMPTY_PAGE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Список лотов</title></head>
<body>
  <table>
    <tbody>
    </tbody>
  </table>
</body>
</html>
"""


def test_esia_title_host_marker_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """Title containing 'esia.gosuslugi.ru' must raise SessionExpiredError."""
    with pytest.raises(SessionExpiredError):
        parser.parse(_ESIA_TITLE_HOST_HTML)


def test_esia_gosuslugi_portal_title_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """HTML with title 'Портал государственных услуг' must raise SessionExpiredError."""
    with pytest.raises(SessionExpiredError):
        parser.parse(_ESIA_HOST_HTML)


def test_gosuslugi_title_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """HTML with title 'Госуслуги' must raise SessionExpiredError."""
    with pytest.raises(SessionExpiredError):
        parser.parse(_GOSUSLUGI_TITLE_HTML)


def test_session_expired_not_parse_bug_error(
    parser: SelectolaxListParser,
) -> None:
    """SessionExpiredError is NOT a subclass of ParseBugError — callers must catch separately."""
    with pytest.raises(SessionExpiredError) as exc_info:
        parser.parse(_ESIA_HOST_HTML)
    assert not isinstance(exc_info.value, ParseBugError), (
        "SessionExpiredError must NOT be a ParseBugError — different recovery paths"
    )


def test_normal_page_with_esia_navlink_not_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """A normal lot-list page with esia.gosuslugi.ru in nav must NOT raise SessionExpiredError.

    The normal site navigation includes an ESIA registration link.
    Only an ESIA-titled redirect page (title contains ESIA markers) should trigger.
    """
    rows = parser.parse(_NORMAL_PAGE_WITH_ESIA_NAVLINK_HTML).rows
    assert rows == []


def test_normal_empty_tbody_does_not_raise_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """A page with a real tbody (even empty) must NOT raise SessionExpiredError."""
    rows = parser.parse(_NORMAL_EMPTY_PAGE_HTML).rows
    assert rows == []


def test_no_tbody_without_esia_raises_parse_bug(
    parser: SelectolaxListParser,
) -> None:
    """A page with no tbody and no ESIA markers must raise ParseBugError (DOM change)."""
    html = "<html><body><p>No table here</p></body></html>"
    with pytest.raises(ParseBugError):
        parser.parse(html)


# ---------------------------------------------------------------------------
# 13. ESIA head-signal detection (Signal 2) — meta refresh in <head>
# ---------------------------------------------------------------------------

# ESIA redirect page where title is neutral but <head> contains a meta refresh
# pointing to esia.gosuslugi.ru — Signal 2 must fire alone.
#
# Example (real-world variant seen on fis.rosim.gov.ru):
#   <head>
#     <title>Переадресация...</title>
#     <meta http-equiv="refresh" content="0; url=https://esia.gosuslugi.ru/login/"/>
#   </head>
_ESIA_HEAD_META_REFRESH_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Переадресация...</title>
  <meta http-equiv="refresh" content="0; url=https://esia.gosuslugi.ru/login/"/>
</head>
<body>
  <p>Перенаправление на портал авторизации...</p>
</body>
</html>
"""

# Page where esia.gosuslugi.ru appears in body nav links only (not in <head>)
# — must NOT trigger SessionExpiredError (false-positive guard).
_ESIA_BODY_LINK_ONLY_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Список свободных лотов</title></head>
<body>
  <nav>
    <a href="https://esia.gosuslugi.ru/registration">Регистрация через ЕСИА</a>
  </nav>
  <table>
    <tbody>
    </tbody>
  </table>
</body>
</html>
"""


def test_esia_head_meta_refresh_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """Signal 2: esia.gosuslugi.ru in <head> <meta http-equiv='refresh'> must
    raise SessionExpiredError even when the <title> contains no ESIA markers.

    This covers a redirect variant where the server sends a neutral title
    ('Переадресация...') but embeds the ESIA URL in a meta-refresh tag.
    """
    with pytest.raises(SessionExpiredError):
        parser.parse(_ESIA_HEAD_META_REFRESH_HTML)


def test_esia_body_link_does_not_raise_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """esia.gosuslugi.ru appearing only in <body> nav links must NOT trigger
    SessionExpiredError — normal lot-list pages include ESIA registration links.

    Verifies that head-signal scoping to <head> prevents false positives.
    """
    rows = parser.parse(_ESIA_BODY_LINK_ONLY_HTML).rows
    assert rows == []


# ---------------------------------------------------------------------------
# 14. ESIA head-signal (Signal 2) — inline script window.location in <head>
# ---------------------------------------------------------------------------

# ESIA redirect page where title is neutral and <head> contains an inline
# <script> with window.location='https://esia.gosuslugi.ru/...'.
# Signal 2 uses node.html (full raw tag HTML) so it catches both src= attributes
# and inline JS content — this test documents and locks that invariant.
_ESIA_HEAD_SCRIPT_INLINE_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Переадресация...</title>
  <script>window.location='https://esia.gosuslugi.ru/login/';</script>
</head>
<body>
  <p>Перенаправление на портал авторизации...</p>
</body>
</html>
"""


def test_esia_head_inline_script_window_location_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """Signal 2 (inline script): window.location pointing to esia.gosuslugi.ru
    inside a <head> <script> tag must raise SessionExpiredError.

    node.html returns the full raw tag HTML including inline content, so
    the esia.gosuslugi.ru string is found without any extra logic.
    """
    with pytest.raises(SessionExpiredError):
        parser.parse(_ESIA_HEAD_SCRIPT_INLINE_HTML)


# ---------------------------------------------------------------------------
# 15. ESIA form-signal detection (Signal 3) — <form action> in <body>
# ---------------------------------------------------------------------------

# ESIA redirect page where title is neutral, no <head> meta/script markers,
# but <body> contains an auto-submitting form whose action points to ESIA.
# Signal 3 must fire alone.
_ESIA_FORM_ACTION_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Переадресация...</title>
</head>
<body>
  <form action="https://esia.gosuslugi.ru/login/oauth2/ac" method="POST">
    <input type="hidden" name="client_id" value="torgi"/>
    <input type="hidden" name="redirect_uri" value="https://torgi.gov.ru/callback"/>
  </form>
  <script>document.forms[0].submit();</script>
</body>
</html>
"""

# Normal page: form points to an internal URL — must NOT trigger Signal 3.
_NORMAL_PAGE_WITH_INTERNAL_FORM_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Список свободных лотов</title></head>
<body>
  <form action="/cabinet/free-lot" method="GET">
    <input type="text" name="q"/>
    <button type="submit">Найти</button>
  </form>
  <table>
    <tbody>
    </tbody>
  </table>
</body>
</html>
"""


def test_esia_form_action_raises_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """Signal 3: <form action='https://esia.gosuslugi.ru/...'> must raise
    SessionExpiredError even when title and <head> contain no ESIA markers.

    Covers POST-based ESIA flows that render an auto-submitting form in the body.
    """
    with pytest.raises(SessionExpiredError):
        parser.parse(_ESIA_FORM_ACTION_HTML)


def test_normal_internal_form_does_not_raise_session_expired(
    parser: SelectolaxListParser,
) -> None:
    """A form with an internal (non-ESIA) action must NOT trigger SessionExpiredError.

    Verifies that Signal 3 is scoped to forms whose action contains
    esia.gosuslugi.ru, not all forms.
    """
    rows = parser.parse(_NORMAL_PAGE_WITH_INTERNAL_FORM_HTML).rows
    assert rows == []


# ---------------------------------------------------------------------------
# 16. total_count extraction
# ---------------------------------------------------------------------------


def test_total_count_from_perpage50_fixture(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    page = parser.parse(html_perpage50)
    assert page.total_count == 404


def test_total_count_from_sorted_desc_fixture(
    parser: SelectolaxListParser, html_sorted_desc: str
) -> None:
    page = parser.parse(html_sorted_desc)
    assert page.total_count == 405


def test_total_count_none_when_no_paginate_info(parser: SelectolaxListParser) -> None:
    html = (
        "<html><body><table>"
        "<thead><tr><th>X</th></tr></thead>"
        "<tbody></tbody>"
        "</table></body></html>"
    )
    page = parser.parse(html)
    assert page.total_count is None


def test_total_count_zero_for_empty_fixture(parser: SelectolaxListParser) -> None:
    html = load_fixture("list_region_empty.html")
    page = parser.parse(html)
    assert page.rows == []
    assert page.total_count == 0


def test_total_count_none_for_malformed_text(parser: SelectolaxListParser) -> None:
    html = (
        "<html><body>"
        "<table><thead><tr><th>X</th></tr></thead><tbody></tbody></table>"
        '<div class="table-paginate__info">Нет данных</div>'
        "</body></html>"
    )
    page = parser.parse(html)
    assert page.total_count is None


def test_parse_returns_parsedlistpage_type(
    parser: SelectolaxListParser, html_perpage50: str
) -> None:
    from fis_monitor.domain.models import ParsedListPage

    page = parser.parse(html_perpage50)
    assert isinstance(page, ParsedListPage)
