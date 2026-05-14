"""Unit tests for EnrichmentService.

Coverage targets (per task spec):
- test_enrich_lots_parallel: 5 lots, max_workers=3, all enriched; both fakes
  called for every lot.
- test_per_lot_http_error_isolated: 1-of-5 raises requests.Timeout; remaining
  4 enriched, failing lot returned with enrichment_status='failed'.
- test_per_lot_parse_bug_error_isolated: 1-of-5 raises ParseBugError; same
  isolation guarantee.
- test_empty_input_returns_empty: enrich_lots([]) → [].
- test_max_workers_1_serial: serial smoke — all lots processed.
- test_url_builder_used: http.get called with correct URL per lot.id.
- test_returned_order_matches_input: output order equals input order.
- test_parser_version_mismatch_propagates: ParserVersionMismatch escapes.

Fake implementation rule (from project orchestrator-playbook): fakes MUST
have a test that calls ALL their methods, not just isinstance().
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest

from fis_monitor.domain.errors import ParseBugError, ParserVersionMismatch
from fis_monitor.domain.models import HttpResponse, ParsedDetail
from fis_monitor.infra.http.url_builder import TorgiUrlBuilder
from fis_monitor.services.enrichment import EnrichmentService
from tests.factories import make_lot

# ---------------------------------------------------------------------------
# Fake implementations
# ---------------------------------------------------------------------------

_FAKE_DETAIL = ParsedDetail(
    lat=55.75,
    lon=37.62,
    has_boundaries=True,
    date_update=None,
    raw_json={"detail": "enriched"},
    parser_version=1,
)


class FakeHttpClient:
    """Queue-based fake — returns pre-configured HttpResponse or raises.

    Design: a per-url dispatch table maps url → response/exception.
    Thread-safe: protected by a lock so tests can introspect call counts from
    the main thread after the pool finishes.

    ``configure`` is a convenience that builds the default-builder URL for a lot_id.
    For explicit URLs, use ``configure_url`` directly.
    """

    _DEFAULT_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")

    def __init__(self) -> None:
        self._responses: dict[str, HttpResponse | BaseException] = {}
        self._calls: list[str] = []
        self._lock = threading.Lock()

    def configure(self, lot_id: int, response: HttpResponse | BaseException) -> None:
        """Register a response/exception for the default builder URL for lot_id."""
        url = self._DEFAULT_BUILDER.lot_detail_url(lot_id=lot_id)
        self._responses[url] = response

    def configure_url(self, url: str, response: HttpResponse | BaseException) -> None:
        """Register a response/exception for an explicit URL."""
        self._responses[url] = response

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        with self._lock:
            self._calls.append(url)
        configured = self._responses.get(url)
        if isinstance(configured, BaseException):
            raise configured
        if configured is not None:
            return configured
        # Default: happy-path HTML response.
        return HttpResponse(
            status=200,
            text="<html>detail</html>",
            headers={},
            final_url=url,
        )

    @property
    def calls(self) -> list[str]:
        with self._lock:
            return list(self._calls)

    def called_lot_ids(self) -> list[int]:
        """Extract lot_id from the detail-page URLs that were called."""
        import re
        ids = []
        for url in self.calls:
            m = re.search(r"[?&]id=(\d+)", url)
            if m:
                ids.append(int(m.group(1)))
        return ids


class FakeDetailParser:
    """Returns a fixed ParsedDetail, or raises for configured lot_ids.

    Thread-safe via lock.
    """

    def __init__(self, detail: ParsedDetail = _FAKE_DETAIL) -> None:
        self._detail = detail
        self._raise_for: dict[int, BaseException] = {}
        self._calls: list[str] = []
        self._lock = threading.Lock()
        # Track which url was passed for each call (we derive lot_id from it
        # via a side-channel set by the service — but in practice the parser
        # only receives HTML, so we track parse() invocations).
        self._parse_calls: list[str] = []

    def configure_raise(self, lot_id: int, exc: BaseException) -> None:
        self._raise_for[lot_id] = exc

    def parse(self, html: str) -> ParsedDetail:
        with self._lock:
            self._parse_calls.append(html)
        # Derive a lot_id marker embedded in the HTML by FakeHttpClient
        # (see _make_html helper below).
        lot_id = _extract_lot_id_from_html(html)
        exc = self._raise_for.get(lot_id)
        if exc is not None:
            raise exc
        return self._detail

    @property
    def parse_count(self) -> int:
        with self._lock:
            return len(self._parse_calls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_html(lot_id: int) -> str:
    """Generate HTML that encodes the lot_id so FakeDetailParser can dispatch."""
    return f"<html><body data-lot-id='{lot_id}'>detail for {lot_id}</body></html>"


def _extract_lot_id_from_html(html: str) -> int:
    """Extract lot_id embedded by _make_html. Returns -1 if not found."""
    import re
    m = re.search(r"data-lot-id='(\d+)'", html)
    return int(m.group(1)) if m else -1


_DEFAULT_BUILDER = TorgiUrlBuilder(base_url="https://xn--80aaggvgieoeoa2bo7l.xn--p1ai")


def _make_http_with_html(lot_ids: list[int]) -> FakeHttpClient:
    """Return a FakeHttpClient that returns HTML encoding lot_id for each lot."""
    client = FakeHttpClient()
    for lot_id in lot_ids:
        url = _DEFAULT_BUILDER.lot_detail_url(lot_id=lot_id)
        client.configure_url(
            url,
            HttpResponse(
                status=200,
                text=_make_html(lot_id),
                headers={},
                final_url=url,
            ),
        )
    return client


def _make_service(
    http: FakeHttpClient | None = None,
    parser: FakeDetailParser | None = None,
) -> tuple[EnrichmentService, FakeHttpClient, FakeDetailParser]:
    http = http or FakeHttpClient()
    parser = parser or FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser)
    return svc, http, parser


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enrich_lots_parallel_all_enriched() -> None:
    """5 lots, max_workers=3 → all 5 enriched, both fakes called per lot."""
    lot_ids = [101, 102, 103, 104, 105]
    lots = [make_lot(id=lid, enrichment_status="pending") for lid in lot_ids]

    http = _make_http_with_html(lot_ids)
    parser = FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser)

    results = svc.enrich_lots(lots, max_workers=3)

    assert len(results) == 5
    for r in results:
        assert r.enrichment_status == "done", f"lot {r.id} not enriched"
        assert r.lat == _FAKE_DETAIL.lat
        assert r.lon == _FAKE_DETAIL.lon

    # Verify both fakes were exercised for every lot.
    called_ids = sorted(http.called_lot_ids())
    assert called_ids == lot_ids, f"http called for: {called_ids}"
    assert parser.parse_count == 5


def test_per_lot_http_error_isolated() -> None:
    """HTTPError on lot index 1 → that lot returns 'failed', others enriched."""
    import requests  # type: ignore[import-untyped]

    lot_ids = [201, 202, 203, 204, 205]
    lots = [make_lot(id=lid, enrichment_status="pending") for lid in lot_ids]

    http = _make_http_with_html(lot_ids)
    # Lot 202 raises a requests.Timeout
    http.configure(202, requests.Timeout("simulated timeout"))

    parser = FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser)

    results = svc.enrich_lots(lots, max_workers=5)

    assert len(results) == 5
    by_id = {r.id: r for r in results}
    assert by_id[202].enrichment_status == "failed"
    for lid in [201, 203, 204, 205]:
        assert by_id[lid].enrichment_status == "done", f"lot {lid} not enriched"


def test_per_lot_parse_bug_error_isolated() -> None:
    """ParseBugError on one lot → that lot returns 'failed', others enriched."""
    lot_ids = [301, 302, 303, 304, 305]
    lots = [make_lot(id=lid, enrichment_status="pending") for lid in lot_ids]

    http = _make_http_with_html(lot_ids)
    parser = FakeDetailParser()
    parser.configure_raise(303, ParseBugError("div.detail-card", "missing main block"))
    svc = EnrichmentService(http=http, parser=parser)

    results = svc.enrich_lots(lots, max_workers=5)

    assert len(results) == 5
    by_id = {r.id: r for r in results}
    assert by_id[303].enrichment_status == "failed"
    for lid in [301, 302, 304, 305]:
        assert by_id[lid].enrichment_status == "done"


def test_empty_input_returns_empty() -> None:
    """enrich_lots([]) returns [] without touching http or parser."""
    svc, http, parser = _make_service()
    result = svc.enrich_lots([], max_workers=4)
    assert result == []
    assert http.called_lot_ids() == []
    assert parser.parse_count == 0


def test_max_workers_1_serial() -> None:
    """max_workers=1 (serial) — all lots processed correctly."""
    lot_ids = [401, 402, 403]
    lots = [make_lot(id=lid, enrichment_status="pending") for lid in lot_ids]
    http = _make_http_with_html(lot_ids)
    parser = FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser)

    results = svc.enrich_lots(lots, max_workers=1)

    assert len(results) == 3
    assert all(r.enrichment_status == "done" for r in results)


def test_url_builder_used() -> None:
    """http.get is called with the URL produced by url_builder.lot_detail_url."""
    custom_builder = TorgiUrlBuilder(base_url="http://localhost:8765")
    lot_ids = [501, 502]
    lots = [make_lot(id=lid) for lid in lot_ids]
    http = FakeHttpClient()
    for lid in lot_ids:
        url = custom_builder.lot_detail_url(lot_id=lid)
        http.configure_url(
            url,
            HttpResponse(
                status=200,
                text=_make_html(lid),
                headers={},
                final_url=url,
            ),
        )
    parser = FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser, url_builder=custom_builder)

    svc.enrich_lots(lots, max_workers=2)

    urls = set(http.calls)
    expected = {custom_builder.lot_detail_url(lot_id=lid) for lid in lot_ids}
    assert urls == expected, f"Unexpected URLs: {urls}"


def test_returned_order_matches_input() -> None:
    """Output list order must equal input order regardless of completion order."""
    lot_ids = list(range(601, 616))  # 15 lots
    lots = [make_lot(id=lid) for lid in lot_ids]
    http = _make_http_with_html(lot_ids)
    parser = FakeDetailParser()
    svc = EnrichmentService(http=http, parser=parser)

    results = svc.enrich_lots(lots, max_workers=8)

    assert [r.id for r in results] == lot_ids


def test_parser_version_mismatch_propagates() -> None:
    """ParserVersionMismatch must NOT be caught — it propagates to caller."""
    lots = [make_lot(id=701)]
    http = _make_http_with_html([701])
    parser = FakeDetailParser()
    parser.configure_raise(701, ParserVersionMismatch("version mismatch"))
    svc = EnrichmentService(http=http, parser=parser)

    with pytest.raises(ParserVersionMismatch):
        svc.enrich_lots(lots, max_workers=1)


# ---------------------------------------------------------------------------
# Fake exhaustiveness: verify ALL fake methods are called (not just isinstance)
# ---------------------------------------------------------------------------


def test_fake_http_client_all_methods_exercised() -> None:
    """FakeHttpClient.get(), configure_url(), configure() — verify all work correctly."""
    client = FakeHttpClient()
    # configure_url: explicit URL
    target_url = _DEFAULT_BUILDER.lot_detail_url(lot_id=999)
    client.configure_url(
        target_url,
        HttpResponse(status=200, text="<html/>", headers={}, final_url=target_url),
    )
    resp = client.get(target_url)
    assert resp.status == 200
    assert client.calls == [target_url]
    assert client.called_lot_ids() == [999]
    # configure: convenience method using default builder URL
    client2 = FakeHttpClient()
    client2.configure(
        111,
        HttpResponse(status=200, text="<html/>", headers={}, final_url=""),
    )
    url_111 = _DEFAULT_BUILDER.lot_detail_url(lot_id=111)
    resp2 = client2.get(url_111)
    assert resp2.status == 200


def test_fake_detail_parser_all_methods_exercised() -> None:
    """FakeDetailParser.parse() is the only public interface method — verify it works."""
    parser = FakeDetailParser()
    detail = parser.parse(_make_html(42))
    assert detail.lat == _FAKE_DETAIL.lat
    assert parser.parse_count == 1
