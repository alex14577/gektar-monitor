"""Unit tests for SseCycleDone publish invariants in MonitorCycleService.

Layer 2 (Application services) — pure unit tests with FakeEventBus per
docs/architecture/09-test-strategy.md. Covers the terminal cycle-completion
signal: exactly one ``SseCycleDone`` per ``run_cycle`` invocation, in every
exit branch (happy path + 3 _close_with_* helpers + session-expired).

Related: gektar_monitor-akqg (cycle: спиннер «Идёт проверка» не очищается).
The frontend listens for ``cycle.done`` SSE events to replace the static
"Идёт проверка" spinner that POST /cycle/run injects into #cycle-result.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.errors import ParseBugError, SessionExpiredError, UpstreamError
from fis_monitor.domain.models import Settings, SseCycleDone, SseCycleError, SseSessionExpired
from tests.unit.services.test_monitor_cycle import (
    _REGION,
    FakeClock,
    FakeConfigSource,
    FakeEnrichmentService,
    FakeHttpClient,
    FakeListParser,
    FakeLotRepository,
    _make_lot,
    _make_parsed_row,
    _make_service,
)


def _cycle_done_events(bus_published: list) -> list[SseCycleDone]:
    return [e for e in bus_published if isinstance(e, SseCycleDone)]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestCycleDoneHappyPath:
    def test_happy_path_publishes_single_cycle_done_ok(self) -> None:
        rows = [_make_parsed_row(i) for i in (1, 2, 3)]
        lots = [_make_lot(i) for i in (1, 2, 3)]

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=rows),
            enrichment=FakeEnrichmentService(lots=lots),
            lot_repo=FakeLotRepository(was_new_for={1, 2}),  # 2 new of 3
        )

        result = svc.run_cycle(_REGION)

        done = _cycle_done_events(bus.published)
        assert len(done) == 1, "exactly one SseCycleDone expected per run_cycle invocation"
        evt = done[0]
        assert evt.status == "ok"
        assert evt.cycle_id == result.id
        assert evt.lots_fetched == 3
        assert evt.new_lots == 2
        assert evt.duration_ms >= 0


# ---------------------------------------------------------------------------
# Error branches — done event ALWAYS follows the critical event
# ---------------------------------------------------------------------------


class TestCycleDoneOnErrorBranches:
    """Each error helper publishes SseCycleError/SseSessionExpired AND
    SseCycleDone(status='error'). The critical event is published first so the
    UI's error-handling SSE swap (cycle.error) wins precedence; cycle.done is
    the terminal "spinner can be cleared" signal regardless of branch.
    """

    def test_upstream_error_publishes_done_after_cycle_error(self) -> None:
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            http=FakeHttpClient(raises=UpstreamError("net", category="network")),
        )

        svc.run_cycle(_REGION)

        # Both events present, critical FIRST.
        kinds = [type(e).__name__ for e in bus.published]
        assert "SseCycleError" in kinds
        assert "SseCycleDone" in kinds
        assert kinds.index("SseCycleError") < kinds.index("SseCycleDone")

        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].status == "error"
        assert done[0].lots_fetched == 0
        assert done[0].new_lots == 0

    def test_parse_bug_publishes_done_after_cycle_error(self) -> None:
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(raises=ParseBugError("sel", "missing")),
        )

        svc.run_cycle(_REGION)

        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].status == "error"
        # Ordering: cycle.error before cycle.done.
        idx_err = next(i for i, e in enumerate(bus.published) if isinstance(e, SseCycleError))
        idx_done = bus.published.index(done[0])
        assert idx_err < idx_done

    def test_session_expired_publishes_done_after_session_expired_event(self) -> None:
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(raises=SessionExpiredError("expired")),
        )

        svc.run_cycle(_REGION)

        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].status == "error"
        idx_session = next(
            i for i, e in enumerate(bus.published) if isinstance(e, SseSessionExpired)
        )
        idx_done = bus.published.index(done[0])
        assert idx_session < idx_done

    def test_unexpected_exception_publishes_done_before_reraise(self) -> None:
        class _Boom(RuntimeError):
            pass

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(raises=_Boom("boom")),
        )

        with pytest.raises(_Boom):
            svc.run_cycle(_REGION)

        # Caller MUST re-raise, BUT the done event must still have been
        # published from inside _close_with_unexpected_error before the
        # re-raise — otherwise the spinner stays forever on internal bugs.
        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].status == "error"


# ---------------------------------------------------------------------------
# Duration invariant
# ---------------------------------------------------------------------------


class TestCycleDoneDurationNonNegative:
    """duration_ms is StrictInt non-negative even when finished_at <= started_at.

    FakeClock returns a fixed timestamp; finished_at == started_at, so duration
    is exactly 0. The clamp to max(0, ...) guards against negative skew when a
    real Clock is used and a system clock adjustment lands inside the cycle.
    """

    def test_zero_duration_when_clock_is_fixed(self) -> None:
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
        )

        svc.run_cycle(_REGION)

        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].duration_ms == 0


# ---------------------------------------------------------------------------
# finished_at_hhmm invariant
# ---------------------------------------------------------------------------


class TestCycleDoneFinishedAtHhmm:
    """finished_at_hhmm must equal the cycle-completion time in the configured timezone.

    Invariant (bd nq5g): the ok-branch UI renders "Проверка завершена в HH:MM";
    HH:MM is the local time derived from clock.now() converted to settings.timezone.
    """

    def test_finished_at_hhmm_reflects_local_timezone(self) -> None:
        # UTC 12:00 → Europe/Moscow (UTC+3) = 15:00
        fixed_utc = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        clock = FakeClock(fixed=fixed_utc)

        config = FakeConfigSource()
        config._settings = Settings(timezone="Europe/Moscow")

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=[_make_parsed_row(1)]),
            enrichment=FakeEnrichmentService(lots=[_make_lot(1)]),
            lot_repo=FakeLotRepository(was_new_for={1}),
            clock=clock,
            config_source=config,
        )

        svc.run_cycle(_REGION)

        done = _cycle_done_events(bus.published)
        assert len(done) == 1
        assert done[0].finished_at_hhmm == "15:00", (
            "UTC 12:00 in Europe/Moscow (UTC+3) must render as '15:00'"
        )


# ---------------------------------------------------------------------------
# HTTP 302 redirect response → session_expired cycle (not parse_bug)
# ---------------------------------------------------------------------------


class TestCycleDoneOnRedirectResponse:
    """HTTP 3xx response before parse → SessionExpired path, not ParseBugError.

    Regression: donor returns 302 with empty body on unauth lot-list request;
    without the redirect-detect guard the empty text would reach the parser
    and produce a ParseBugError cycle instead of a session_expired cycle.
    """

    def test_302_empty_body_produces_session_expired_cycle(self) -> None:
        svc, _, _, _, _, _, _, bus, _ = _make_service(
            http=FakeHttpClient(response_text="", response_status=302),
        )

        result = svc.run_cycle(_REGION)

        assert result.status == "error"
        assert result.error == "session_expired"
        assert result.lots_fetched == 0

        kinds = [type(e).__name__ for e in bus.published]
        assert "SseSessionExpired" in kinds
        assert "SseCycleDone" in kinds
        # SseSessionExpired before SseCycleDone
        assert kinds.index("SseSessionExpired") < kinds.index("SseCycleDone")
        # No ParseBugError / SseCycleError published
        assert "SseCycleError" not in kinds
