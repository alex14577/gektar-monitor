"""Unit tests for GET /events SSE endpoint.

Coverage:
  #1  — No Origin header → 200 + text/event-stream
  #2  — Origin in whitelist → 200 + streaming response
  #3  — Origin NOT in whitelist → 421 Misdirected Request (no stream)
  #4  — Bad origin body is not an SSE stream
  #5  — get_sse_streamer DI override works
  #6  — get_csrf_origin_whitelist DI override works
  #7  — Schema drift: unknown event type → dropped, audit log has sse.schema_drift
  #8  — All fake interface methods are exercised (anti-mock invariant)
  #9  — Loopback origin (http://127.0.0.1:8080) → allowed
  #10 — Response has Cache-Control: no-cache
  #11 — Response has X-Accel-Buffering: no
  #12 — Origin matching is case-insensitive
  #13 — Empty frozenset whitelist → all origins rejected
  #14 — test #14 (canonical SSE acceptance): bad origin → 421, no stream body
  #15 — SSE Content-Type is text/event-stream
  #16 — SseLotNew with fragment_template='poster' → HTML fragment, not JSON
  #17 — SseLotNew with unsupported fragment_template → JSON fallback, no crash
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    SseEvent,
    SseLotNew,
    SseLotStatus,
    SsePayloadSchema,
)
from fis_monitor.infra.sse.sse_stream import _KNOWN_SSE_EVENTS, SseStreamer
from fis_monitor.web.deps import get_csrf_origin_whitelist, get_sse_streamer
from fis_monitor.web.routes.events import router
from fis_monitor.web.sse_encoder import make_html_sse_encoder
from fis_monitor.web.templates import build_templates

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_WHITELIST: frozenset[str] = frozenset(
    {
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://[::1]:8080",
    }
)


def _make_lot_new(lot_id: int = 1) -> SseLotNew:
    lot = Lot(
        id=lot_id,
        cadastral_no="01:02:000000:1",
        area_sqm=None,
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
    )
    dto = LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")
    return SseLotNew(lot=dto, fragment_template="poster")


# ---------------------------------------------------------------------------
# Fake EventSubscription
# ---------------------------------------------------------------------------


class _FakeSubscription:
    """Fake EventSubscription — all interface methods exercised in tests."""

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


# ---------------------------------------------------------------------------
# Fake EventBus
# ---------------------------------------------------------------------------


class _FakeEventBus:
    """Fake EventBus — all interface methods implemented."""

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self._subscriptions: list[_FakeSubscription] = []

    def publish(self, event: SseEvent) -> None:
        for sub in self._subscriptions:
            sub._events.append(event)

    def subscribe(self) -> _FakeSubscription:
        sub = _FakeSubscription(list(self._events))
        self._subscriptions.append(sub)
        return sub


# ---------------------------------------------------------------------------
# Schema-drift fake: SseStreamer that injects schema drift logging
#
# The known-event set mirrors the closed SseEvent union.  Any event whose
# 'event' discriminator is NOT in _KNOWN_SSE_EVENTS is treated as drift:
# dropped and logged.  This is a defence-in-depth guard for future event
# types added to the bus without a corresponding route/UI update.
# ---------------------------------------------------------------------------

# Known event discriminators for the fake _DriftTrackingStreamer.  Includes "ping"
# (keep-alive frame, not an SseEvent union member but valid on the wire).
_FAKE_KNOWN_SSE_EVENTS: frozenset[str] = frozenset(
    {"lot.new", "lot.status", "session.expired", "cycle.error", "smtp.failed", "ping"}
)


class _DriftTrackingStreamer:
    """Fake SseStreamer that records sse.schema_drift log records.

    Drift = an event whose discriminator is not in ``_KNOWN_SSE_EVENTS``.
    Known events are yielded as minimal SSE frames.
    """

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self.drift_logged: list[str] = []

    async def stream(self) -> AsyncIterator[bytes]:
        for event in self._events:
            event_type = getattr(event, "event", type(event).__name__)
            if event_type not in _FAKE_KNOWN_SSE_EVENTS:
                # Unknown event → drop + log schema drift (fail-closed)
                logging.getLogger(__name__).error(
                    "sse.schema_drift",
                    extra={"event_type": event_type},
                )
                self.drift_logged.append(event_type)
                continue
            # Yield a minimal event frame so tests can assert delivery
            yield f"event: {event_type}\ndata: {{}}\n\n".encode()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    *,
    streamer: Any,
    whitelist: frozenset[str] = _WHITELIST,
) -> FastAPI:
    """Build minimal FastAPI app with the events router and DI overrides."""
    app = FastAPI()
    app.include_router(router)

    app.dependency_overrides[get_sse_streamer] = lambda: streamer
    app.dependency_overrides[get_csrf_origin_whitelist] = lambda: whitelist

    return app


# ---------------------------------------------------------------------------
# Minimal SseStreamer that yields a single event then closes
# ---------------------------------------------------------------------------


def _make_finite_streamer(events: list[SseEvent]) -> SseStreamer:
    """SseStreamer backed by a fake bus that drains finite events and closes."""
    bus = _FakeEventBus(events)
    executor = ThreadPoolExecutor(max_workers=1)
    return SseStreamer(event_bus=bus, sse_executor=executor, ping_interval=0.05)


# ===========================================================================
# Tests
# ===========================================================================


class TestOriginCheck:
    """#1-#4, #9, #12-#14: Origin header validation."""

    def test_no_origin_returns_streaming(self) -> None:
        """#1 — No Origin header → 200 text/event-stream (allowed)."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]

    def test_valid_origin_returns_streaming(self) -> None:
        """#2 — Origin in whitelist → 200."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream(
            "GET", "/events", headers={"Origin": "http://127.0.0.1:8080"}
        ) as resp:
            assert resp.status_code == 200

    def test_bad_origin_returns_421(self) -> None:
        """#3 / #14 — Bad Origin → 421 before stream opens."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        resp = client.get(
            "/events", headers={"Origin": "http://evil.example.com"}
        )
        assert resp.status_code == 421

    def test_bad_origin_body_is_not_sse(self) -> None:
        """#4 — 421 response is plain text, not an SSE stream."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        resp = client.get(
            "/events", headers={"Origin": "http://evil.example.com"}
        )
        assert resp.status_code == 421
        assert "text/event-stream" not in resp.headers.get("content-type", "")

    def test_loopback_origin_allowed(self) -> None:
        """#9 — Loopback origin is in whitelist → 200."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream(
            "GET", "/events", headers={"Origin": "http://localhost:8080"}
        ) as resp:
            assert resp.status_code == 200

    def test_origin_case_insensitive(self) -> None:
        """#12 — Origin matching is case-insensitive."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream(
            "GET", "/events", headers={"Origin": "HTTP://127.0.0.1:8080"}
        ) as resp:
            assert resp.status_code == 200

    def test_empty_whitelist_rejects_all_origins(self) -> None:
        """#13 — Empty whitelist: any explicit Origin is rejected."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(
            _build_app(streamer=streamer, whitelist=frozenset()),
            raise_server_exceptions=True,
        )
        resp = client.get(
            "/events", headers={"Origin": "http://127.0.0.1:8080"}
        )
        assert resp.status_code == 421

    def test_empty_whitelist_no_origin_still_ok(self) -> None:
        """#13b — Empty whitelist: missing Origin is still allowed."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(
            _build_app(streamer=streamer, whitelist=frozenset()),
            raise_server_exceptions=True,
        )
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200

    def test_origin_null_rejected(self) -> None:
        """m4 — Origin: null (sandbox iframe) → 421 Misdirected Request.

        Browsers send ``Origin: null`` for sandboxed iframes and ``data:`` URIs.
        ``null`` is not in the loopback whitelist, so it must be rejected.
        """
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        resp = client.get("/events", headers={"Origin": "null"})
        assert resp.status_code == 421


class TestResponseHeaders:
    """#10-#11: Response headers for SSE."""

    def test_cache_control_no_cache(self) -> None:
        """#10 — Cache-Control: no-cache."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "no-cache"

    def test_x_accel_buffering_no(self) -> None:
        """#11 — X-Accel-Buffering: no (disables nginx proxy buffering)."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            assert resp.headers.get("x-accel-buffering") == "no"


class TestDIOverrides:
    """#5-#6: DI provider override contract."""

    def test_get_sse_streamer_override_used(self) -> None:
        """#5 — app.dependency_overrides[get_sse_streamer] is called."""
        called: list[bool] = []

        class _TrackingStreamer(_DriftTrackingStreamer):
            async def stream(self) -> AsyncIterator[bytes]:
                called.append(True)
                return
                yield  # make it an async generator

        streamer = _TrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events"):
            pass
        assert called, "SseStreamer.stream() was never called"

    def test_get_csrf_origin_whitelist_override_used(self) -> None:
        """#6 — Whitelist override is respected."""
        custom = frozenset({"http://custom.local:9000"})
        streamer = _DriftTrackingStreamer([])
        client = TestClient(
            _build_app(streamer=streamer, whitelist=custom),
            raise_server_exceptions=True,
        )
        # custom.local:9000 is in whitelist → OK
        with client.stream(
            "GET", "/events", headers={"Origin": "http://custom.local:9000"}
        ) as resp:
            assert resp.status_code == 200

        # 127.0.0.1:8080 is NOT in custom whitelist → 421
        resp2 = client.get(
            "/events", headers={"Origin": "http://127.0.0.1:8080"}
        )
        assert resp2.status_code == 421


class TestSchemaDrift:
    """#7: Unknown event type → dropped, sse.schema_drift logged."""

    def test_unknown_event_dropped_and_drift_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#7 -- Schema drift: unknown event type is dropped, not streamed;
        audit log contains 'sse.schema_drift'.

        Regression guard via _DriftTrackingStreamer (fake) — verifies the fake
        itself has correct drift semantics. See #7b for production-streamer coverage.
        """
        # SsePayloadSchema.for_event is the PII-whitelist for critical events.
        # Drift detection uses _KNOWN_SSE_EVENTS derived from the SseEvent union.
        assert SsePayloadSchema.for_event("unknown.drift.event") == frozenset(), (
            "SsePayloadSchema.for_event must return empty frozenset for unknown types"
        )

        # Craft an event whose discriminator is NOT in _KNOWN_SSE_EVENTS.
        ghost_event = MagicMock(spec=[])
        ghost_event.event = "ghost.event"  # not in _KNOWN_SSE_EVENTS
        ghost_event.priority = "normal"

        streamer = _DriftTrackingStreamer([ghost_event])  # type: ignore[list-item]

        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with caplog.at_level(logging.ERROR), client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            # Read all chunks -- ghost event was dropped so stream is empty
            chunks = list(resp.iter_bytes(chunk_size=1024))

        all_bytes = b"".join(chunks)
        assert b"ghost.event" not in all_bytes, (
            "Drifted event must be dropped from the stream"
        )
        assert streamer.drift_logged == ["ghost.event"]

    def test_production_streamer_drops_drift_event_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#7b — Production SseStreamer drops unknown event type + logs sse.schema_drift.

        Uses _make_finite_streamer (real SseStreamer) and monkey-patches an
        SseLotStatus instance to inject an invalid discriminator, bypassing
        Pydantic's frozen guard via object.__setattr__.
        """
        # Known event first to verify stream works; then the ghost event.
        known = SseLotStatus(lot_id=7, new_status="gone", event_type="gone")
        # Inject a drifted event: clone known but override `event` discriminator.
        drifted = SseLotStatus(lot_id=99, new_status="active", event_type="changed")
        object.__setattr__(drifted, "event", "ghost.future")  # bypass frozen

        streamer = _make_finite_streamer([known, drifted])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)

        with caplog.at_level(logging.ERROR), client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes(chunk_size=1024))

        all_bytes = b"".join(chunks)
        assert b"lot.status" in all_bytes, "Known event must appear in stream"
        assert b"ghost.future" not in all_bytes, "Drifted event must not reach the wire"

        drift_records = [r for r in caplog.records if r.message == "sse.schema_drift"]
        assert drift_records, "Production SseStreamer must log sse.schema_drift for unknown events"
        assert drift_records[0].__dict__.get("event_type") == "ghost.future"

    def test_known_event_is_streamed(self) -> None:
        """Schema-compliant event is NOT dropped."""
        known = SseLotStatus(lot_id=42, new_status="gone", event_type="gone")
        streamer = _DriftTrackingStreamer([known])

        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes(chunk_size=1024))

        all_bytes = b"".join(chunks)
        assert b"lot.status" in all_bytes, "Known event must appear in stream"
        assert streamer.drift_logged == [], "No drift for a known event"

    def test_known_sse_events_derived_from_union(self) -> None:
        """_KNOWN_SSE_EVENTS must include all five concrete SseEvent discriminators."""
        expected = {"lot.new", "lot.status", "cycle.error", "smtp.failed", "session.expired"}
        assert expected <= _KNOWN_SSE_EVENTS, (
            f"Missing discriminators in _KNOWN_SSE_EVENTS: {expected - _KNOWN_SSE_EVENTS}"
        )


class TestFakeInterfaces:
    """#8: Anti-mock — all fake interface methods are called at least once."""

    def test_fake_subscription_all_methods_called(self) -> None:
        """All _FakeSubscription methods are exercised (anti-mock invariant)."""
        events: list[SseEvent] = [
            SseLotStatus(lot_id=1, new_status="gone", event_type="gone")
        ]
        sub = _FakeSubscription(events)

        # wait_one: returns event then None (sets alive=False)
        ev1 = sub.wait_one(timeout=0.1)
        assert ev1 is events[0]
        ev2 = sub.wait_one(timeout=0.1)
        assert ev2 is None
        assert sub.alive is False

        # iter: returns []
        extra = sub.iter()
        assert isinstance(extra, list)

        # unsubscribe: marks unsubscribed
        sub.unsubscribe()
        assert sub.unsubscribed is True

    def test_fake_event_bus_all_methods_called(self) -> None:
        """All _FakeEventBus methods are exercised."""
        bus = _FakeEventBus([])

        # subscribe
        sub = bus.subscribe()
        assert isinstance(sub, _FakeSubscription)

        # publish: appends to subscription
        ev = SseLotStatus(lot_id=99, new_status="active", event_type="changed")
        bus.publish(ev)
        assert sub._events == [ev]


class TestHtmlSseEncoding:
    """#15-#16: HTML-rendering encoder for lot.new events.

    Layer 4 (Web) — integration via TestClient + FakeEventBus.
    Invariants per docs/architecture/09-test-strategy.md Layer 4 §SSE:
      #15: SSE endpoint Content-Type is text/event-stream.
      #16: SseLotNew with fragment_template='poster' → client receives an
           HTML fragment (<article …>), NOT a JSON string.
    """

    def test_content_type_is_text_event_stream(self) -> None:
        """#15 — GET /events returns Content-Type: text/event-stream."""
        streamer = _DriftTrackingStreamer([])
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]

    def test_lot_new_poster_renders_html_not_json(self) -> None:
        """#16 — SseLotNew(fragment_template='poster') → HTML fragment in SSE data.

        Uses the real Jinja2 environment (build_templates()) and the real
        make_html_sse_encoder so the assertion covers the actual rendering path.
        """
        lot_new = _make_lot_new(lot_id=42)
        streamer = _make_finite_streamer([lot_new])

        # Inject the HTML-rendering encoder (mirrors lifespan wiring in app.py).
        templates = build_templates()
        streamer.bind_event_encoder(make_html_sse_encoder(templates.env))

        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes(chunk_size=4096))

        payload = b"".join(chunks).decode()

        # Must contain HTML article element (lot poster partial).
        assert "<article" in payload, (
            "Expected HTML <article> element in SSE data for lot.new poster event"
        )
        # Must NOT contain raw JSON event envelope.
        assert '"event":"lot.new"' not in payload and '"event": "lot.new"' not in payload, (
            "SSE data must be HTML, not the raw JSON SseLotNew payload"
        )
        # SSE line discipline (RFC 8895): every non-empty line must start with
        # a recognised field prefix.
        for line in payload.split("\n"):
            if line and not line.startswith(("event:", "data:", ":")):
                raise AssertionError(f"SSE line discipline violated: {line!r}")

    def test_unsupported_fragment_template_does_not_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#17 — SseLotNew with unsupported fragment_template → JSON fallback, no crash.

        The encoder logs a warning and falls back to JSON encoding.
        The stream must remain open (status 200) and the payload must not
        contain rendered HTML with broken Jinja2 Undefined fields.
        Mirrors the object.__setattr__ injection technique from test #7b.
        """
        lot_new = _make_lot_new(lot_id=99)
        # fragment_template="list" is no longer in the Literal, so we bypass
        # Pydantic's frozen guard to inject the unsupported value (same pattern
        # as #7b for ghost discriminators).
        object.__setattr__(lot_new, "fragment_template", "list")

        streamer = _make_finite_streamer([lot_new])
        templates = build_templates()
        streamer.bind_event_encoder(make_html_sse_encoder(templates.env))

        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with caplog.at_level(logging.WARNING), client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes(chunk_size=4096))

        payload = b"".join(chunks).decode()

        # JSON fallback: the raw event envelope is present (not HTML).
        assert "<article" not in payload, (
            "Unsupported fragment_template must NOT produce HTML output"
        )
        # Warning must be logged by the encoder.
        warning_records = [
            r for r in caplog.records
            if r.message == "sse_encoder.unknown_fragment_template"
        ]
        assert warning_records, "Encoder must log a warning for unsupported fragment_template"

    def test_poster_shows_published_at_human(self) -> None:
        """Poster renders lot.date_create via published_at_human inline in .lot__cad."""
        from datetime import UTC, datetime

        pub_dt = datetime(2026, 3, 14, 9, 5, tzinfo=UTC)
        lot_new = _make_lot_new(lot_id=7)
        object.__setattr__(lot_new.lot, "date_create", pub_dt)

        streamer = _make_finite_streamer([lot_new])
        templates = build_templates()
        streamer.bind_event_encoder(make_html_sse_encoder(templates.env))

        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            chunks = list(resp.iter_bytes(chunk_size=4096))

        payload = b"".join(chunks).decode()
        assert "14.03.2026 09:05" in payload, (
            "Poster must render lot.date_create as published_at_human in .lot__appeared span"
        )
        assert "chip--new" not in payload, "NEW chip must be absent from poster"
        assert 'data-action="star"' not in payload, "Star button must be absent from poster"
        assert "▼ Детали" not in payload, "Expand button must be absent from poster"
        assert "lot__appeared" in payload, "published_at must appear in .lot__appeared span"


# ---------------------------------------------------------------------------
# #18 — SseCycleDone HTML rendering for #cycle-result spinner clear
# ---------------------------------------------------------------------------


class TestSseCycleDoneHtmlEncoding:
    """gektar_monitor-akqg: SseCycleDone → HTML fragment via _cycle_done.html.jinja.

    The frontend listener (#cycle-done-listener in base.html.jinja) swaps the
    rendered HTML into #cycle-result, replacing the static "Идёт проверка"
    spinner from POST /cycle/run.
    """

    def _emit(self, event: SseEvent) -> str:
        from fis_monitor.web.sse_encoder import make_html_sse_encoder

        streamer = _make_finite_streamer([event])
        templates = build_templates()
        streamer.bind_event_encoder(make_html_sse_encoder(templates.env))
        client = TestClient(_build_app(streamer=streamer), raise_server_exceptions=True)
        with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes(chunk_size=4096))
        return b"".join(chunks).decode()

    def test_cycle_done_ok_renders_ok_span_with_counters(self) -> None:
        from fis_monitor.domain.models import SseCycleDone

        evt = SseCycleDone(
            timestamp=_TS,
            cycle_id=42,
            status="ok",
            lots_fetched=12,
            new_lots=3,
            duration_ms=1400,
        )

        payload = self._emit(evt)

        assert "event: cycle.done" in payload
        assert "cycle-result--ok" in payload
        # Counters in the rendered fragment so the user sees a concrete result.
        assert "12" in payload and "3" in payload
        # Duration rendered in seconds with one decimal (1400 ms → 1.4 с).
        assert "1.4" in payload
        # No JSON envelope leakage.
        assert '"event":"cycle.done"' not in payload

    def test_cycle_done_error_renders_err_span(self) -> None:
        from fis_monitor.domain.models import SseCycleDone

        evt = SseCycleDone(
            timestamp=_TS,
            cycle_id=99,
            status="error",
            lots_fetched=0,
            new_lots=0,
            duration_ms=0,
        )

        payload = self._emit(evt)

        assert "event: cycle.done" in payload
        assert "cycle-result--err" in payload
        # Error message present, counters NOT rendered for error variant.
        assert "ошибкой" in payload
