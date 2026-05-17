"""SelectolaxDetailParser -- parses the /cabinet/free-lot-view?id=N detail page.

DOM structure (from fixture detail_lot_9990.html):
  .request-declaration__block-left__sub--doc
    .request-domain__key-value
      div > div (label) + div (value)  [Shirata, Dolgota, Region, Address]

  .request-declaration__block-footer  (multiple)
    .request-domain__key-value
      div > div (label) + div (value)  [Cadastral no, Boundaries,
        Date of registration, Date of EGRN update]

Typed fields:
  lat            DMS string -> decimal float (Shirata/Lat)
  lon            DMS string -> decimal float (Dolgota/Lon)
  has_boundaries "Est" -> True, "Net" -> False, absent -> None
  date_registry  DD.MM.YYYY -> datetime UTC  ("Дата постановки на учет" — ЕГРН reg. date)
  date_update    DD.MM.YYYY -> datetime UTC  ("Дата изменения сведений в ЕГРН")

raw_json stores all extracted key-value pairs for forward-compat.

Parser invariants (R3-minor):
  - absent / empty fields -> None, NEVER ""
  - stateless, no logging, no PII in ParseBugError
  - Coordinates absent -> lat=lon=None (NOT 0.0)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser, Node

from fis_monitor.domain.errors import ParseBugError
from fis_monitor.domain.models import ParsedDetail

_DATE_FORMAT = "%d.%m.%Y"

# Matches DMS coordinates after HTML entity decode.
# Degree sign U+00B0, Prime (minutes) U+2032, Double prime (seconds) U+2033
# Use chr() to avoid RUF001 ambiguous-character warnings for these Unicode chars.
_DEGREE = chr(0x00B0)  # degree sign
_PRIME = chr(0x2032)  # prime (minutes)
_DOUBLE_PRIME = chr(0x2033)  # double prime (seconds)
_DMS_PAT = (
    r"(\d+)\s*"
    + _DEGREE
    + r"\s*(\d+)\s*["
    + _PRIME
    + r"']\s*(\d+(?:\.\d+)?)\s*["
    + _DOUBLE_PRIME
    + r"\"]?"
)
_DMS_RE = re.compile(_DMS_PAT)


def _dms_to_decimal(dms_str: str) -> float | None:
    """Convert DMS string to decimal degrees."""
    if not dms_str:
        return None
    m = _DMS_RE.search(dms_str)
    if not m:
        return None
    try:
        deg = float(m.group(1))
        minutes = float(m.group(2))
        secs = float(m.group(3))
        return deg + minutes / 60.0 + secs / 3600.0
    except (ValueError, AttributeError):
        return None


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


def _text_or_none(node: Node | None) -> str | None:
    """Return stripped text of node, or None if absent/empty."""
    if node is None:
        return None
    text = node.text(strip=True)
    return text if text else None


def _extract_kv_pairs(container: Node) -> dict[str, str]:
    """Extract key-value pairs from a .request-domain__key-value container.

    Each pair is a direct-child <div> of ``container`` containing two child
    <div>s: first is the label, second is the value.

    Iterating only direct children (not all descendants via ``.css("div")``)
    prevents duplicate entries when nested div structures appear inside a
    pair's value cell.
    """
    result: dict[str, str] = {}
    for pair_div in container.iter(include_text=False):
        # container.iter() yields only direct children — no parent== guard needed.
        # (selectolax Node.__eq__ uses Python identity, not DOM pointer equality,
        # so `pair_div.parent == container` is unreliable across wrapper instances.)
        if pair_div.tag != "div":
            continue
        direct_divs = [c for c in pair_div.iter(include_text=False) if c.tag == "div"]
        if len(direct_divs) >= 2:
            label = direct_divs[0].text(strip=True)
            value = direct_divs[1].text(strip=True)
            if label:
                result[label] = value
    return result


def _extract_all_kv(tree: HTMLParser) -> dict[str, str]:
    """Extract all key-value pairs from the detail page.

    Two patterns handled:
    1. Standard div-pair inside .request-domain__key-value (label + value divs).
    2. h3.request-declaration__title + .request-domain__key-value where the
       kv block contains a single value (no label div) -- the h3 is the label.
       Example: "Status" / "Number of citizens offered".
    """
    combined: dict[str, str] = {}

    # Pattern 1: div-pair kv blocks
    for kv_block in tree.css(".request-domain__key-value"):
        pairs = _extract_kv_pairs(kv_block)
        combined.update(pairs)

    # Pattern 2: h3 label + single-value kv block (sections like "Status")
    selector = ".request-declaration__block-footer, .request-declaration__block-left__item--left"
    for block in tree.css(selector):
        h3 = block.css_first("h3.request-declaration__title")
        if h3 is None:
            continue
        label = h3.text(strip=True)
        if not label:
            continue
        kv = block.css_first(".request-domain__key-value")
        if kv is None:
            continue
        # Single-value pattern: top-level div contains one child div with the value
        top_divs = [c for c in kv.iter(include_text=False) if c.tag == "div"]
        for top_div in top_divs:
            inner_divs = [c for c in top_div.iter(include_text=False) if c.tag == "div"]
            if len(inner_divs) == 1:
                value = inner_divs[0].text(strip=True)
                if value and label not in combined:
                    combined[label] = value
            elif len(inner_divs) == 0:
                value = top_div.text(strip=True)
                if value and label not in combined:
                    combined[label] = value

    return combined


class SelectolaxDetailParser:
    """Parse the free-lot detail card page into a ParsedDetail.

    Stateless. ``__init__`` takes no arguments. Thread-safe (no shared state).

    parser_version = 1  (class constant -- increment when typed fields change).
    """

    parser_version: int = 1

    def parse(self, html: str) -> ParsedDetail:
        """Parse a detail-card HTML page into a ParsedDetail.

        Raises:
            ParseBugError: if the expected main content section is absent.
        """
        tree = HTMLParser(html)

        # The main detail block is inside .request-declaration__block-main.
        # If absent, the page DOM has changed or this is not a detail page.
        main_block = tree.css_first(".request-declaration__block-main")
        if main_block is None:
            raise ParseBugError(
                selector=".request-declaration__block-main",
                context="detail-card page; main block missing — site DOM may have changed",
            )

        # --- Collect all key-value pairs across all blocks ---
        all_kv = _extract_all_kv(tree)

        # --- Typed fields ---

        # Coordinates: site uses Cyrillic labels
        lat_raw = all_kv.get("Широта", "")  # Shirota
        lon_raw = all_kv.get("Долгота", "")  # Dolgota
        lat = _dms_to_decimal(lat_raw) if lat_raw else None
        lon = _dms_to_decimal(lon_raw) if lon_raw else None

        # has_boundaries: "Granitsy uchastka"
        boundaries_key = "Границы участка"
        boundaries_raw = all_kv.get(boundaries_key, "")
        yes_val = "Есть"  # "Est'"
        no_val = "Нет"  # "Net"
        if boundaries_raw == yes_val:
            has_boundaries: bool | None = True
        elif boundaries_raw == no_val:
            has_boundaries = False
        else:
            has_boundaries = None

        # date_registry: "Data postanovki na uchet" (EGRN registration date)
        date_registry_key = "Дата постановки на учет"
        date_registry_raw = all_kv.get(date_registry_key, "")
        date_registry = _parse_date(date_registry_raw) if date_registry_raw else None

        # date_update: "Data izmeneniya svedeniy v EGRN"
        date_update_key = "Дата изменения сведений в ЕГРН"
        date_update_raw = all_kv.get(date_update_key, "")
        date_update = _parse_date(date_update_raw) if date_update_raw else None

        # --- raw_json: all extracted kv pairs for forward-compat ---
        raw_json: dict[str, Any] = {k: v for k, v in all_kv.items() if v}

        return ParsedDetail(
            lat=lat,
            lon=lon,
            has_boundaries=has_boundaries,
            date_registry=date_registry,
            date_update=date_update,
            raw_json=raw_json,
            parser_version=self.parser_version,
        )
