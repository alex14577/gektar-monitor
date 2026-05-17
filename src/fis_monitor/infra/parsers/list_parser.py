"""SelectolaxListParser -- parses the /cabinet/free-lot list page HTML.

Column mapping (data-col-seq):
  0  cadastral_no  <a> text inside td
  1  area_sqm      "3 998 kv.m" -> strip suffix + spaces -> int
  2  region        Subject RF
  3  municipality  Municipal formation (may be empty -> None)
  4  settlement    (skipped -- not in ParsedListRow)
  5  address       (skipped)
  6  count         (skipped)
  7  land_category Land category
  8  permitted_use Permitted use
  9  ogv           Responsible OGV -- use title attr for full name
  10 date_create   DD.MM.YYYY
  11 data_source   (skipped)
  12 date_update   DD.MM.YYYY (may be empty -> None)
  13 status        freeLotStatus text

Lot ID comes from <tr data-key="N">.

Parser invariants (R3-minor):
  - absent / empty fields -> None, NEVER ""
  - stateless, no logging, no PII in ParseBugError
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from selectolax.parser import HTMLParser, Node

from fis_monitor.domain.errors import ParseBugError, SessionExpiredError
from fis_monitor.domain.models import ParsedListPage, ParsedListRow

# Hard cap protecting against pathological HTML; FIS lists rarely exceed 100 per page.
_MAX_ROWS_PER_PAGE: Final[int] = 10_000

# Markers that identify an ESIA (Gosuslugi) login-redirect response.
# The site returns HTTP 200 with a login page when the session cookie is expired.
#
# Detection strategy — three independent signals (OR-logic, defense-in-depth):
#
# Signal 1 — <title> tag.
#   ESIA redirect pages carry a distinctive title not present on normal lot-list
#   pages.  We check for _ESIA_HOST_MARKER ("esia.gosuslugi.ru") and
#   _ESIA_TITLE_MARKERS ("Портал государственных услуг", "Госуслуги").
#
# Signal 2 — ESIA URL inside <head> meta/script tags.
#   Some ESIA redirect variants embed a <meta http-equiv="refresh" content="0; url=...
#   esia.gosuslugi.ru/..."> or a <script> redirect in the <head>.  The <script>
#   check uses the raw tag HTML (node.html), which covers both src= attributes and
#   inline JS such as ``window.location='https://esia.gosuslugi.ru/...'``.
#   Searching only the <head> avoids false positives from esia.gosuslugi.ru links
#   in the site navigation that appear on normal lot-list pages.
#
# Example redirect pages (signal 2):
#   <head>
#     <title>Переадресация...</title>
#     <meta http-equiv="refresh" content="0; url=https://esia.gosuslugi.ru/login/"/>
#   </head>
#
#   <head>
#     <title>Переадресация...</title>
#     <script>window.location='https://esia.gosuslugi.ru/login/';</script>
#   </head>
#
# Signal 3 — <form action="https://esia.gosuslugi.ru/..."> anywhere in the document.
#   Some POST-based ESIA flows render a form that auto-submits to the login endpoint.
#   This appears in the <body>, so it is not covered by the <head>-scoped Signal 2.
#   We check only <form> elements (not all body links) to avoid false positives from
#   the esia.gosuslugi.ru navigation links that appear on normal lot-list pages.
#
# Any signal firing → SessionExpiredError (re-auth required).
_ESIA_HOST_MARKER = "esia.gosuslugi.ru"
_ESIA_TITLE_MARKERS = ("Портал государственных услуг", "Госуслуги")

_DATE_FORMAT = "%d.%m.%Y"
# Non-breaking space (U+00A0) used as digit separator on the site
_NBSP = chr(0xa0)


def _parse_date(s: str | None) -> datetime | None:
    """Parse DD.MM.YYYY string to UTC-aware datetime (midnight UTC)."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, _DATE_FORMAT).date()
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    except ValueError:
        return None


def _parse_area(s: str | None) -> int | None:
    """Parse area string to int sqm, or None.

    Handles formats like "3 998 kv.m" (with regular and non-breaking spaces).
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # Remove cyrillic suffix and whitespace digit separators
    s = s.replace("кв.м", "").strip()
    # Remove both regular space (0x20) and non-breaking space (U+00A0)
    digits = s.replace(" ", "").replace(_NBSP, "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _text_or_none(node: Node | None) -> str | None:
    """Return stripped text of node, or None if absent/empty."""
    if node is None:
        return None
    text = node.text(strip=True)
    return text if text else None


class SelectolaxListParser:
    """Parse the free-lot list page into a list of ParsedListRow.

    Stateless. ``__init__`` takes no arguments. Thread-safe (no shared state).
    """

    def parse(self, html: str) -> ParsedListPage:
        """Parse the lot-list HTML page into structured rows.

        Returns an empty list for a valid page that happens to be empty
        (e.g. no lots match the filter).

        Raises:
            ParseBugError: if the expected DOM structure is absent (tbody
                missing from the page where it is required).
        """
        tree = HTMLParser(html)

        # Detect ESIA session-expiry redirect BEFORE the tbody check.
        # The site returns HTTP 200 with a Gosuslugi login page when the
        # session cookie is expired — this is NOT a DOM bug, it is an auth
        # failure. Raising SessionExpiredError lets callers (MonitorCycleService,
        # FullScanService) log WARN and trigger re-auth instead of escalating
        # a misleading ParseBugError.
        #
        # Detection is title-based: the <title> of the ESIA redirect page
        # contains distinctive markers not present in normal lot-list pages.
        # We do NOT search the full HTML body because normal lot-list pages
        # include esia.gosuslugi.ru links in the navigation header.
        title_node = tree.css_first("title")
        title_text = title_node.text(strip=True) if title_node is not None else ""
        _title_signal = _ESIA_HOST_MARKER in title_text or any(
            marker in title_text for marker in _ESIA_TITLE_MARKERS
        )

        # Signal 2: esia.gosuslugi.ru URL present inside <head> <meta> or <script>
        # tags.  Scoped to <head> to avoid false positives from nav-bar links that
        # appear on normal lot-list pages.
        _head_signal = False
        head_node = tree.css_first("head")
        if head_node is not None:
            for tag in ("meta", "script"):
                for node in head_node.css(tag):
                    node_html = node.html or ""
                    if _ESIA_HOST_MARKER in node_html:
                        _head_signal = True
                        break
                if _head_signal:
                    break

        # Signal 3: <form action="https://esia.gosuslugi.ru/..."> anywhere in the
        # document.  POST-based ESIA flows render an auto-submitting form whose
        # action attribute points to the login endpoint.  The form lives in the
        # <body>, so it is not reachable by the <head>-scoped Signal 2.  We search
        # all <form> tags rather than the full HTML text to avoid false positives
        # from esia.gosuslugi.ru registration links in the site navigation.
        _form_signal = any(
            _ESIA_HOST_MARKER in (node.attributes.get("action") or "")
            for node in tree.css("form")
        )

        if _title_signal or _head_signal or _form_signal:
            raise SessionExpiredError(
                "Session expired: response contains ESIA login-page markers"
            )

        # The list table uses <tbody> wrapping <tr data-key="N"> rows.
        # A missing tbody indicates a DOM change or a non-list page.
        tbody = tree.css_first("tbody")
        if tbody is None:
            raise ParseBugError(
                selector="tbody",
                context="lot-list page; tbody missing — site DOM may have changed",
            )

        rows: list[ParsedListRow] = []
        for tr in tbody.css("tr[data-key]"):
            lot_id_str = tr.attributes.get("data-key", "")
            if not lot_id_str:
                continue
            try:
                lot_id = int(lot_id_str)
            except ValueError:
                continue

            tds = tr.css("td")
            if len(tds) < 14:
                # Unexpected structure -- skip malformed row defensively
                continue

            # col 0: cadastral_no -- inside <a> link
            cadastral_node = tds[0].css_first("a")
            if cadastral_node is not None:
                cadastral_no_raw = _text_or_none(cadastral_node)
            else:
                cadastral_no_raw = _text_or_none(tds[0])
            cadastral_no = cadastral_no_raw or ""

            # col 1: area_sqm
            area_sqm = _parse_area(_text_or_none(tds[1]))

            # col 2: region
            region = _text_or_none(tds[2]) or ""

            # col 3: municipality
            municipality = _text_or_none(tds[3])

            # col 7: land_category
            land_category = _text_or_none(tds[7])

            # col 8: permitted_use
            permitted_use = _text_or_none(tds[8])

            # col 9: ogv -- prefer title attribute (full name), fall back to text
            ogv_title = tds[9].attributes.get("title", "")
            if ogv_title and ogv_title.strip():
                ogv: str | None = ogv_title.strip()
            else:
                ogv = _text_or_none(tds[9])

            # col 10: date_create
            date_create = _parse_date(_text_or_none(tds[10]))
            if date_create is None:
                # date_create is required -- skip this row if unparseable
                continue

            # col 12: date_update
            date_update = _parse_date(_text_or_none(tds[12]))

            # col 13: status
            status = _text_or_none(tds[13]) or ""

            rows.append(
                ParsedListRow(
                    id=lot_id,
                    cadastral_no=cadastral_no,
                    area_sqm=area_sqm,
                    region=region,
                    municipality=municipality,
                    land_category=land_category,
                    permitted_use=permitted_use,
                    ogv=ogv,
                    status=status,
                    date_create=date_create,
                    date_update=date_update,
                )
            )
            if len(rows) >= _MAX_ROWS_PER_PAGE:
                raise ParseBugError(
                    selector="tr[data-key]",
                    context=f"row cap exceeded: {_MAX_ROWS_PER_PAGE}",
                )

        total_count: int | None = None
        info_node = tree.css_first(".table-paginate__info")
        if info_node is not None:
            m = re.search(r"Найдено записей:\s*(\d+)\s*из\s*\d+", info_node.text(strip=True))
            if m is not None:
                total_count = int(m.group(1))

        return ParsedListPage(rows=rows, total_count=total_count)
