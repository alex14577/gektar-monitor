"""Logging tests for MonitorCycleService DEBUG events (b9wq).

Covers: cycle.start, region.fetch.start/finish, region.upsert, cycle.finish.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any  # used in expected_fields type hints

import pytest

from fis_monitor.domain.models import ParsedListRow
from fis_monitor.services.monitor_cycle import MonitorCycleService
from tests.fakes.lot_repository import FakeLotRepository
from tests.unit.services.conftest import (
    MinimalClock,
    MinimalConfigSource,
    MinimalCyclesRepository,
    MinimalEnrichmentService,
    MinimalEventBus,
    MinimalHttpClient,
    MinimalListParser,
    MinimalNotifierDispatcher,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_REGION = 77
_LOGGER = "fis_monitor.services.monitor_cycle"
_ROW = ParsedListRow(
    id=101,
    cadastral_no="77:01:00000001:1",
    area_sqm=1000,
    region="77",
    municipality="Москва",
    land_category="Земли населённых пунктов",
    permitted_use="ИЖС",
    ogv="ДГИ",
    status="PUBLISHED",
    date_create=_NOW,
    date_update=_NOW,
)


def _make_svc(rows: list[ParsedListRow] | None = None) -> MonitorCycleService:
    return MonitorCycleService(
        http=MinimalHttpClient(),
        list_parser=MinimalListParser(rows=rows),
        enrichment=MinimalEnrichmentService(),
        lot_repo=FakeLotRepository(),
        cycles_repo=MinimalCyclesRepository(),
        notifier_dispatcher=MinimalNotifierDispatcher(),
        event_bus=MinimalEventBus(),
        config_source=MinimalConfigSource(),
        clock=MinimalClock(),
        cycle_progress_signal=threading.Event(),
    )


_CYCLE_PARAMS = [
    pytest.param(
        "monitor_cycle.cycle.start",
        None,
        {"region_id": _REGION},
        id="cycle.start",
    ),
    pytest.param(
        "monitor_cycle.region.fetch.start",
        None,
        {"region_id": _REGION, "cycle_id": ...},
        id="fetch.start",
    ),
    pytest.param(
        "monitor_cycle.region.fetch.finish",
        None,
        {"http_status": 200, "duration_ms": ...},
        id="fetch.finish",
    ),
    pytest.param(
        "monitor_cycle.cycle.finish",
        None,
        {"status": "ok", "lots_fetched": ..., "new_lots": ...},
        id="cycle.finish",
    ),
    pytest.param(
        "monitor_cycle.region.upsert",
        [_ROW],
        {"lot_id": 101, "region_id": _REGION, "was_new": False},
        id="region.upsert",
    ),
]


@pytest.mark.parametrize("event_name,rows,expected_fields", _CYCLE_PARAMS)
def test_run_cycle_emits_debug_event(
    event_name: str,
    rows: list[ParsedListRow] | None,
    expected_fields: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each DEBUG event is emitted with required structured extra fields."""
    svc = _make_svc(rows=rows)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        svc.run_cycle(_REGION)

    records = [r for r in caplog.records if r.getMessage() == event_name]
    assert records, f"expected {event_name!r} debug event"

    rec = records[0]
    for field, expected in expected_fields.items():
        assert field in rec.__dict__, f"missing field {field!r} in {event_name!r}"
        if expected is not ...:
            assert rec.__dict__[field] == expected, (
                f"{event_name!r} field {field!r}: expected {expected!r}, "
                f"got {rec.__dict__[field]!r}"
            )
