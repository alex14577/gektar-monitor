"""Tests for SseStatus(state='checking') publication (bd zb3).

Layer 2 — Application services.  Uses fakes from test_monitor_cycle.py.

Invariants covered:
  (1) SseStatus(state='checking') is published before the HTTP call.
  (2) SseStatus(state='active') is published after a successful cycle.
  (3) The checking event precedes the cycle.done / active events.
"""

from __future__ import annotations

from fis_monitor.domain.models import SseCycleDone, SseStatus
from tests.unit.services.test_monitor_cycle import (
    _REGION,
    FakeEnrichmentService,
    FakeListParser,
    FakeLotRepository,
    _make_lot,
    _make_parsed_row,
    _make_service,
)


class TestCheckingStatusPublication:
    def test_checking_published_before_http_and_active_after(self) -> None:
        rows = [_make_parsed_row(1)]
        lots = [_make_lot(1)]

        svc, _, _, _, _, _, _, bus, _ = _make_service(
            list_parser=FakeListParser(rows=rows),
            enrichment=FakeEnrichmentService(lots=lots),
            lot_repo=FakeLotRepository(was_new_for={1}),
        )

        svc.run_cycle(_REGION)

        events = list(bus.published)
        status_events = [e for e in events if isinstance(e, SseStatus)]
        assert len(status_events) >= 2, (
            "at least checking + active SseStatus events expected"
        )

        states = [e.state for e in status_events]
        assert "checking" in states, "SseStatus(state='checking') must be published"
        assert "active" in states, (
            "SseStatus(state='active') must be published after cycle"
        )

        checking_idx = next(
            i for i, e in enumerate(events)
            if isinstance(e, SseStatus) and e.state == "checking"
        )
        done_idx = next(
            i for i, e in enumerate(events) if isinstance(e, SseCycleDone)
        )
        active_idx = next(
            i for i, e in enumerate(events)
            if isinstance(e, SseStatus) and e.state == "active"
        )

        assert checking_idx < done_idx, "checking must precede cycle.done"
        assert checking_idx < active_idx, "checking must precede active"

        last_checking_idx = max(
            i for i, e in enumerate(events)
            if isinstance(e, SseStatus) and e.state == "checking"
        )
        assert last_checking_idx < active_idx, (
            "no checking event may appear after the terminal active event"
        )
