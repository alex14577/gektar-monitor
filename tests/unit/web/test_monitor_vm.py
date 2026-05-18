"""Unit tests for ``web.monitor_vm.build_monitor_vm`` (bd 47uh)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fis_monitor.domain.models import Settings
from fis_monitor.web.monitor_vm import build_monitor_vm
from tests.fakes.lot_repository import FakeLotRepository

_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)


def _session(
    *, expired: bool = False, expires_soon: bool = False, hhmm: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        expired=expired, expires_soon=expires_soon, expires_at_hhmm=hhmm,
    )


def test_state_active_when_session_ok() -> None:
    vm = build_monitor_vm(
        settings=Settings(interval_minutes=5),
        session=_session(),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )
    assert vm.state == "active"
    assert vm.interval_minutes == 5


def test_state_warning_when_expires_soon() -> None:
    vm = build_monitor_vm(
        settings=Settings(),
        session=_session(expires_soon=True, hhmm="14:30"),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )
    assert vm.state == "warning"
    assert vm.expires_at_hhmm == "14:30"


def test_state_error_overrides_warning() -> None:
    vm = build_monitor_vm(
        settings=Settings(),
        session=_session(expired=True, expires_soon=True),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )
    assert vm.state == "error"


def test_last_new_human_dash_when_db_empty() -> None:
    vm = build_monitor_vm(
        settings=Settings(),
        session=_session(),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )
    assert vm.last_new_human == "—"


def test_last_new_human_real_age_from_repo() -> None:
    """bd 47uh acceptance: chip reflects actual MAX(first_seen)."""
    from fis_monitor.domain.models import Lot

    repo = FakeLotRepository()
    five_min_ago = _NOW - timedelta(minutes=5)
    lot = Lot.model_construct(
        id=1,
        cadastral_no="00:00:000000:1",
        area_sqm=None,
        region="Test",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=_NOW,
        date_update=None,
        date_registry=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        parser_version=1,
        first_seen=five_min_ago,
        last_seen=_NOW,
        detail_fetched_at=None,
        enrichment_status="pending",
        last_seen_at=_NOW,
        is_active=True,
        inactive_reason=None,
        inactive_since=None,
        inactive_confirmed_at=None,
        region_id=None,
    )
    repo._lots[1] = lot

    vm = build_monitor_vm(
        settings=Settings(),
        session=_session(),
        lot_repo=repo,
        now=_NOW,
    )
    assert vm.last_new_human == "5 мин назад"
