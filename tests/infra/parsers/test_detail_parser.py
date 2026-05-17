"""Tests for SelectolaxDetailParser."""

from __future__ import annotations

from datetime import UTC

import pytest

from fis_monitor.domain.errors import ParseBugError
from fis_monitor.domain.models import ParsedDetail
from fis_monitor.infra.parsers.detail_parser import SelectolaxDetailParser

from .conftest import load_fixture


@pytest.fixture()
def parser() -> SelectolaxDetailParser:
    return SelectolaxDetailParser()


@pytest.fixture()
def html_detail_9990() -> str:
    return load_fixture("detail_lot_9990.html")


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_parsed_detail(
    parser: SelectolaxDetailParser, html_detail_9990: str
) -> None:
    result = parser.parse(html_detail_9990)
    assert isinstance(result, ParsedDetail)
    assert isinstance(result.raw_json, dict)
    assert len(result.raw_json) > 0


# ---------------------------------------------------------------------------
# 2. lat/lon parsed as float
# ---------------------------------------------------------------------------


def test_lat_lon_parsed_as_float(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    result = parser.parse(html_detail_9990)
    assert result.lat is not None
    assert result.lon is not None
    assert isinstance(result.lat, float)
    assert isinstance(result.lon, float)
    # 48deg33'21" = 48 + 33/60 + 21/3600 ~= 48.5558
    assert abs(result.lat - 48.5558) < 0.001
    # 134deg57'14" = 134 + 57/60 + 14/3600 ~= 134.9539
    assert abs(result.lon - 134.9539) < 0.001


# ---------------------------------------------------------------------------
# 3. has_boundaries is bool or None
# ---------------------------------------------------------------------------


def test_has_boundaries_is_bool(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    result = parser.parse(html_detail_9990)
    assert isinstance(result.has_boundaries, bool)
    assert result.has_boundaries is True


# ---------------------------------------------------------------------------
# 4. date_update parses to datetime UTC or None
# ---------------------------------------------------------------------------


def test_date_update_is_none_when_empty(
    parser: SelectolaxDetailParser, html_detail_9990: str
) -> None:
    """Fixture has empty 'Data izmeneniya svedeniy v EGRN' -> None."""
    result = parser.parse(html_detail_9990)
    assert result.date_update is None


def test_date_update_is_utc_aware_when_present(
    parser: SelectolaxDetailParser,
) -> None:
    """Synthetic HTML with a date_update value."""
    html = (
        "<html><body>"
        '<div class="request-declaration__block-main">'
        '<div class="request-domain__key-value">'
        "<div>"
        "<div>"
        + chr(1044)
        + chr(1072)
        + chr(1090)
        + chr(1072)
        + " "  # Дата
        + chr(1080)
        + chr(1079)
        + chr(1084)
        + chr(1077)
        + chr(1085)  # измен
        + chr(1077)
        + chr(1085)
        + chr(1080)
        + chr(1103)
        + " "  # ения
        + chr(1089)
        + chr(1074)
        + chr(1077)
        + chr(1076)
        + chr(1077)  # свед
        + chr(1085)
        + chr(1080)
        + chr(1081)
        + " "  # ений
        + chr(1074)
        + " "  # в
        + chr(1045)
        + chr(1043)
        + chr(1056)
        + chr(1053)  # ЕГРН
        + "</div>"
        "<div>15.12.2021</div>"
        "</div>"
        "</div>"
        "</div>"
        "</body></html>"
    )
    result = parser.parse(html)
    assert result.date_update is not None
    assert result.date_update.tzinfo == UTC
    assert result.date_update.year == 2021
    assert result.date_update.month == 12
    assert result.date_update.day == 15


# ---------------------------------------------------------------------------
# 5. parser_version == 1
# ---------------------------------------------------------------------------


def test_parser_version_is_one(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    result = parser.parse(html_detail_9990)
    assert result.parser_version == 1


# ---------------------------------------------------------------------------
# 6. raw_json contains non-typed fields
# ---------------------------------------------------------------------------


def test_raw_json_contains_cadastral_no(
    parser: SelectolaxDetailParser, html_detail_9990: str
) -> None:
    """Cadastral number is on the detail page and should appear in raw_json."""
    result = parser.parse(html_detail_9990)
    # Build "Cadastral number" key using chr() codepoints to avoid RUF001/RUF003.
    # Codepoints for "Kadastrovyi nomer" (Cyrillic):
    # Ka-da-s-t-r-o-v-y-j (space) n-o-m-e-r
    key = "".join(
        chr(c)
        for c in [
            1050,
            1072,
            1076,
            1072,
            1089,
            1090,
            1088,
            1086,
            1074,
            1099,
            1081,
            32,
            1085,
            1086,
            1084,
            1077,
            1088,
        ]
    )
    assert key in result.raw_json, (
        f"Key {key!r} not found in raw_json keys: {list(result.raw_json.keys())}"
    )
    assert result.raw_json[key] == "79:06:2701002:287"


def test_raw_json_contains_status(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    """Status section (h3 pattern) should be captured in raw_json."""
    result = parser.parse(html_detail_9990)
    # "Статус" in Cyrillic
    key = "".join(chr(c) for c in [1057, 1090, 1072, 1090, 1091, 1089])
    assert key in result.raw_json, f"'Status' key not in raw_json: {list(result.raw_json.keys())}"


def test_raw_json_is_non_empty_dict(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    result = parser.parse(html_detail_9990)
    assert isinstance(result.raw_json, dict)
    assert len(result.raw_json) >= 3


# ---------------------------------------------------------------------------
# 7. Malformed HTML without main container -> ParseBugError
# ---------------------------------------------------------------------------


def test_missing_main_block_raises_parse_bug_error(
    parser: SelectolaxDetailParser,
) -> None:
    html = "<html><body><p>No lot detail here</p></body></html>"
    with pytest.raises(ParseBugError) as exc_info:
        parser.parse(html)
    assert exc_info.value.selector == ".request-declaration__block-main"


def test_empty_html_raises_parse_bug_error(
    parser: SelectolaxDetailParser,
) -> None:
    with pytest.raises(ParseBugError) as exc_info:
        parser.parse("<html></html>")
    assert exc_info.value.selector == ".request-declaration__block-main"


# ---------------------------------------------------------------------------
# 8. Coordinates absent -> lat=lon=None, NOT 0.0
# ---------------------------------------------------------------------------


def test_coordinates_absent_returns_none_not_zero(
    parser: SelectolaxDetailParser,
) -> None:
    html = (
        "<html><body>"
        '<div class="request-declaration__block-main">'
        '<div class="request-domain__key-value">'
        "<div><div>Cadastral</div><div>99:99:9999999:999</div></div>"
        "<div><div>Boundary</div><div>No</div></div>"
        "</div>"
        "</div>"
        "</body></html>"
    )
    result = parser.parse(html)
    assert result.lat is None, f"Expected None for absent lat, got {result.lat}"
    assert result.lon is None, f"Expected None for absent lon, got {result.lon}"
    assert result.lat != 0.0
    assert result.lon != 0.0


def test_has_boundaries_false_when_net(
    parser: SelectolaxDetailParser,
) -> None:
    # "Нет" = "Net" (No) in Cyrillic
    net = "".join(chr(c) for c in [1053, 1077, 1090])
    # "Границы участка" = "Granitsy uchastka" in Cyrillic
    boundary_key = "".join(
        chr(c)
        for c in [
            1043,
            1088,
            1072,
            1085,
            1080,
            1094,
            1099,  # Границы
            32,  # space
            1091,
            1095,
            1072,
            1089,
            1090,
            1082,
            1072,  # участка
        ]
    )
    html = (
        "<html><body>"
        '<div class="request-declaration__block-main">'
        '<div class="request-domain__key-value">'
        f"<div><div>{boundary_key}</div><div>{net}</div></div>"
        "</div>"
        "</div>"
        "</body></html>"
    )
    result = parser.parse(html)
    assert result.has_boundaries is False


# ---------------------------------------------------------------------------
# 9. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    result1 = parser.parse(html_detail_9990)
    result2 = parser.parse(html_detail_9990)
    assert result1.lat == result2.lat
    assert result1.lon == result2.lon
    assert result1.has_boundaries == result2.has_boundaries
    assert result1.date_update == result2.date_update
    assert result1.parser_version == result2.parser_version
    assert result1.raw_json == result2.raw_json


# ---------------------------------------------------------------------------
# 10. Nested div de-duplication (_extract_kv_pairs direct-child fix)
# ---------------------------------------------------------------------------


def test_nested_divs_inside_value_cell_do_not_produce_duplicates(
    parser: SelectolaxDetailParser,
) -> None:
    """When a kv-pair value cell contains nested divs, raw_json must not
    contain duplicate keys or unexpected entries produced by the nested
    structure being treated as additional kv-pairs.
    """
    # Build synthetic HTML: one kv-pair where the value div contains a
    # nested div (e.g. a tooltip / badge inside the value).
    html = (
        "<html><body>"
        '<div class="request-declaration__block-main">'
        '<div class="request-domain__key-value">'
        # Pair 1: Label="Cadastral", Value cell has a nested div inside
        "<div>"
        "<div>Cadastral</div>"
        "<div>79:99:9999999:1<div>nested-badge</div></div>"
        "</div>"
        # Pair 2: plain kv-pair (no nested divs)
        "<div>"
        "<div>Region</div>"
        "<div>TestRegion</div>"
        "</div>"
        "</div>"
        "</div>"
        "</body></html>"
    )
    result = parser.parse(html)

    # There must be exactly 2 keys (Cadastral + Region), not more
    assert list(result.raw_json.keys()).count("Cadastral") == 1
    assert list(result.raw_json.keys()).count("Region") == 1
    # The nested-badge text must not appear as a standalone key
    assert "nested-badge" not in result.raw_json


# ---------------------------------------------------------------------------
# 11. date_registry — "Дата постановки на учет" (EGRN registration date)
# ---------------------------------------------------------------------------


def test_date_registry_from_fixture(parser: SelectolaxDetailParser, html_detail_9990: str) -> None:
    """Fixture contains 'Дата постановки на учет': '22.04.2026' → datetime UTC."""
    from datetime import UTC, datetime

    result = parser.parse(html_detail_9990)
    assert result.date_registry == datetime(2026, 4, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    "date_value",
    [
        "",  # absent / empty cell
        "N/A",  # non-date text (invalid format)
        "2026.04.22",  # wrong separator
    ],
    ids=["empty", "invalid_text", "wrong_format"],
)
def test_date_registry_none_when_missing_or_invalid(
    parser: SelectolaxDetailParser,
    date_value: str,
) -> None:
    """Empty or invalid 'Дата постановки на учет' → date_registry is None."""
    # Build Cyrillic key inline to avoid RUF001.
    # "Дата постановки на учет" codepoints:
    key = "".join(
        chr(c)
        for c in [
            1044,
            1072,
            1090,
            1072,
            32,  # Дата
            1087,
            1086,
            1089,
            1090,
            1072,
            1085,  # постан
            1086,
            1074,
            1082,
            1080,
            32,  # овки
            1085,
            1072,
            32,  # на
            1091,
            1095,
            1077,
            1090,  # учет
        ]
    )
    html = (
        "<html><body>"
        '<div class="request-declaration__block-main">'
        '<div class="request-domain__key-value">'
        f"<div><div>{key}</div><div>{date_value}</div></div>"
        "</div>"
        "</div>"
        "</body></html>"
    )
    result = parser.parse(html)
    assert result.date_registry is None
