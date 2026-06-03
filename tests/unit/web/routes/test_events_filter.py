"""Layer 4 integration tests — SSE endpoint view-filter propagation.

Tests the full path: cookie → ViewFiltersService.deserialize →
make_sse_view_filter → SseStreamer.stream(event_filter=...).

Uses TestClient + a finite fake SseStreamer so the stream terminates.
No Jinja rendering; events are encoded via the default JSON encoder
(``encode_sse_event``) so we can assert on ``lot.new`` presence/absence.

Coverage per ADR-052 brainstorm test plan:
  #F1  subject match → event passes
  #F2  subject mismatch → event suppressed
  #F3  area_min match → event passes
  #F4  area_min out of range → suppressed
  #F5  area_sqm=None → pass (fail-open)
  #F7  only_new=True → lot.new passes
  #F8  empty/no cookie → all events pass
  #F9  malformed cookie → fallback default → all events pass
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    SseEvent,
    SseLotNew,
    SseLotStatus,
)
from fis_monitor.infra.sse.sse_stream import SseStreamer
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService, serialize
from fis_monitor.web.deps import (
    get_csrf_origin_whitelist,
    get_region_subscription_repo,
    get_sse_streamer,
    get_view_filters_service,
)
from fis_monitor.web.routes.events import router

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_WHITELIST: frozenset[str] = frozenset()  # no origin check in filter tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lot_new(
    lot_id: int = 1,
    region_id: int | None = None,
    area_sqm: int | None = 10_000,
) -> SseLotNew:
    lot = Lot(
        id=lot_id,
        cadastral_no="01:02:000000:1",
        area_sqm=area_sqm,
        region="TestRegion",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=_TS,
        date_update=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        first_seen=_TS,
        last_seen=_TS,
        detail_fetched_at=None,
        enrichment_status=None,
        last_seen_at=None,
        region_id=region_id,
    )
    dto = LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")
    return SseLotNew(lot=dto, fragment_template="poster")


def _cookie_for(vf: ViewFilters) -> str:
    """Serialize ViewFilters to the percent-encoded cookie string."""
    return serialize(vf)


# ---------------------------------------------------------------------------
# Fake EventSubscription and EventBus
# ---------------------------------------------------------------------------


class _FakeSubscription:
    def __init__(self, events: list[SseEvent]) -> None:
        self._events = list(events)
        self._pos = 0
        self.alive = True
        self.unsubscribed = False

    def wait_one(self, timeout: float) -> SseEvent | None:
        if self._pos < len(self._events):
            ev = self._events[self._pos]
            self._pos += 1
            return ev
        self.alive = False
        return None

    def iter(self) -> list[SseEvent]:
        return []

    def unsubscribe(self) -> None:
        self.unsubscribed = True
        self.alive = False


class _FakeEventBus:
    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events

    def publish(self, event: SseEvent) -> None:  # pragma: no cover
        pass

    def subscribe(self) -> _FakeSubscription:
        return _FakeSubscription(list(self._events))


class _FakeRegionSubRepo:
    def __init__(self, subscribed: frozenset[int]) -> None:
        self._subscribed = subscribed

    def list_subscribed_region_ids(self) -> frozenset[int]:
        return self._subscribed


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_finite_streamer(events: list[SseEvent]) -> SseStreamer:
    bus = _FakeEventBus(events)
    executor = ThreadPoolExecutor(max_workers=1)
    return SseStreamer(event_bus=bus, sse_executor=executor, ping_interval=0.05)


def _build_app(
    *,
    streamer: SseStreamer,
    cookie: str | None = None,
    subscribed_ids: frozenset[int] | None = None,
) -> FastAPI:
    """Build minimal FastAPI app with the events router and DI overrides."""
    repo = _FakeRegionSubRepo(subscribed_ids if subscribed_ids is not None else frozenset())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_sse_streamer] = lambda: streamer
    app.dependency_overrides[get_csrf_origin_whitelist] = lambda: _WHITELIST
    app.dependency_overrides[get_view_filters_service] = lambda: ViewFiltersService()
    app.dependency_overrides[get_region_subscription_repo] = lambda: repo
    return app


def _stream_payload(
    streamer: SseStreamer,
    *,
    cookie: str | None = None,
    subscribed_ids: frozenset[int] | None = None,
) -> str:
    """Run GET /events with optional cookie and return full response body."""
    app = _build_app(streamer=streamer, subscribed_ids=subscribed_ids)
    cookies = {"view_filters": cookie} if cookie else {}
    client = TestClient(app, raise_server_exceptions=True)
    with client.stream("GET", "/events", cookies=cookies) as resp:
        assert resp.status_code == 200
        chunks = list(resp.iter_bytes(chunk_size=4096))
    return b"".join(chunks).decode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSseViewFilterIntegration:
    """Layer 4: SSE endpoint propagates view-filter cookie to SseStreamer."""

    def test_f1_subject_match_event_passes(self) -> None:
        """#F1 — subjects=["34"] + lot.region_id=34 → event in stream."""
        vf = ViewFilters(subjects=["34"])
        lot_new = _make_lot_new(region_id=34)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf), subscribed_ids=frozenset([34]))

        assert "lot.new" in payload, "lot.new event must be in stream when subject matches"

    def test_f2_subject_mismatch_event_suppressed(self) -> None:
        """#F2 — subjects=["34"] + lot.region_id=27 → event suppressed."""
        vf = ViewFilters(subjects=["34"])
        lot_new = _make_lot_new(region_id=27)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.new" not in payload, "lot.new event must be suppressed when subject mismatches"

    def test_f3_area_min_match_passes(self) -> None:
        """#F3 — area_min=1000 + area_sqm=5000 → event in stream."""
        vf = ViewFilters(area_min=1000)
        lot_new = _make_lot_new(area_sqm=5000)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.new" in payload

    def test_f4_area_min_out_of_range_suppressed(self) -> None:
        """#F4 — area_min=10000 + area_sqm=500 → event suppressed."""
        vf = ViewFilters(area_min=10_000)
        lot_new = _make_lot_new(area_sqm=500)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.new" not in payload

    def test_f5_area_sqm_none_passes(self) -> None:
        """#F5 — area_min set + area_sqm=None → pass (fail-open, enrichment pending)."""
        vf = ViewFilters(area_min=1000)
        lot_new = _make_lot_new(area_sqm=None)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.new" in payload, "area_sqm=None must pass-through (fail-open)"

    def test_f7_only_new_passes_lot_new(self) -> None:
        """#F7 — only_new=True → lot.new passes (no-op for SSE)."""
        vf = ViewFilters(only_new=True)
        lot_new = _make_lot_new()
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.new" in payload, "only_new=True must not suppress lot.new"

    def test_f8_no_cookie_all_events_pass(self) -> None:
        """#F8 — no cookie → all events pass (no filter applied)."""
        lot_new = _make_lot_new(region_id=None, area_sqm=None)
        streamer = _make_finite_streamer([lot_new])

        payload = _stream_payload(streamer, cookie=None)

        assert "lot.new" in payload, "missing cookie must result in pass-through"

    def test_f9_malformed_cookie_fallback_passes(self) -> None:
        """#F9 — malformed cookie → fallback to default → all events pass."""
        lot_new = _make_lot_new(region_id=None, area_sqm=None)
        streamer = _make_finite_streamer([lot_new])

        # Deliberately malformed (not valid JSON after decode)
        payload = _stream_payload(streamer, cookie="%7Bnot_valid_json")

        assert "lot.new" in payload, "malformed cookie must fall back to pass-through"

    def test_non_lot_new_events_always_pass_even_with_filter(self) -> None:
        """Non-SseLotNew events pass through regardless of view-filter."""
        vf = ViewFilters(subjects=["34"])
        lot_status = SseLotStatus(lot_id=99, new_status="gone", event_type="gone")
        streamer = _make_finite_streamer([lot_status])

        payload = _stream_payload(streamer, cookie=_cookie_for(vf))

        assert "lot.status" in payload, "lot.status must always pass regardless of view-filter"

    def test_empty_cookie_string_fallback_passes(self) -> None:
        """Empty cookie string (no value) → pass-through."""
        lot_new = _make_lot_new()
        streamer = _make_finite_streamer([lot_new])

        # empty string should be treated as absent
        payload = _stream_payload(streamer, cookie="")

        assert "lot.new" in payload


class TestSseMembershipFilterIntegration:
    """Layer 4: SSE endpoint enforces membership filter (ADR-065)."""

    def test_w1_unsubscribed_live_suppressed(self) -> None:
        """W1: live SseLotNew with unsubscribed region_id is suppressed."""
        lot_new = _make_lot_new(region_id=42)
        streamer = _make_finite_streamer([lot_new])
        payload = _stream_payload(streamer, subscribed_ids=frozenset([10]))
        assert "lot.new" not in payload

    def test_w2_subscribed_passes(self) -> None:
        """W2: live SseLotNew with subscribed region_id passes."""
        lot_new = _make_lot_new(region_id=10)
        streamer = _make_finite_streamer([lot_new])
        payload = _stream_payload(streamer, subscribed_ids=frozenset([10]))
        assert "lot.new" in payload

    def test_w3_backfill_unsubscribed_suppressed(self) -> None:
        """W3: backfill SseLotNew (is_backfill=True) with unsubscribed region suppressed."""
        lot = _make_lot_new(region_id=99)
        backfill_event = SseLotNew(lot=lot.lot, fragment_template="poster", is_backfill=True)
        streamer = _make_finite_streamer([backfill_event])
        payload = _stream_payload(streamer, subscribed_ids=frozenset([10]))
        assert "lot.new" not in payload

    def test_w4_region_id_none_passes(self) -> None:
        """W4: lot.region_id=None passes even with a non-empty subscribed set."""
        lot_new = _make_lot_new(region_id=None)
        streamer = _make_finite_streamer([lot_new])
        payload = _stream_payload(streamer, subscribed_ids=frozenset([10]))
        assert "lot.new" in payload

    def test_w5_empty_subscription_region_lot_suppressed(self) -> None:
        """W5: empty subscribed set → region-bearing lot suppressed."""
        lot_new = _make_lot_new(region_id=55)
        streamer = _make_finite_streamer([lot_new])
        payload = _stream_payload(streamer, subscribed_ids=frozenset())
        assert "lot.new" not in payload

    def test_w6_non_lot_new_unaffected(self) -> None:
        """W6: non-lot.new event (lot.status) passes through regardless of membership."""
        lot_status = SseLotStatus(lot_id=99, new_status="gone", event_type="gone")
        streamer = _make_finite_streamer([lot_status])
        payload = _stream_payload(streamer, subscribed_ids=frozenset())
        assert "lot.status" in payload
