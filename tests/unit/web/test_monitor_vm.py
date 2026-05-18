"""Unit tests for ``web.monitor_vm.build_monitor_vm`` (bd 47uh, bd r82m)."""

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


# ---------------------------------------------------------------------------
# bd r82m: next_fire_at_iso on initial render VM
# ---------------------------------------------------------------------------


def test_next_fire_at_iso_empty_on_initial_render() -> None:
    """build_monitor_vm always returns next_fire_at_iso='' (initial render).

    The scheduler does not expose a next-fire probe API at initial render time.
    The SSE SseStatus event populates next_fire_at after each cycle.
    """
    vm = build_monitor_vm(
        settings=Settings(interval_minutes=5),
        session=_session(),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )
    assert hasattr(vm, "next_fire_at_iso"), (
        "VM must expose next_fire_at_iso so the template can render data-next-check-at"
    )
    assert vm.next_fire_at_iso == ""


# ---------------------------------------------------------------------------
# bd r82m: _header_status.html.jinja renders data-next-check-at attribute
# ---------------------------------------------------------------------------


def test_header_status_template_renders_data_next_check_at_from_sse_status() -> None:
    """SseStatus with next_fire_at → template renders non-empty data-next-check-at.

    Layer 3 (web/templates) contract test: the Jinja2 partial must expose the
    ``data-next-check-at`` attribute when ``monitor.next_fire_at_iso`` is set.
    """
    from datetime import UTC, datetime

    from fis_monitor.domain.models import SseStatus
    from fis_monitor.web.templates import build_templates

    ts = datetime(2026, 5, 18, 10, 30, 0, tzinfo=UTC)
    next_fire = datetime(2026, 5, 18, 10, 31, 0, tzinfo=UTC)
    evt = SseStatus(
        timestamp=ts,
        state="active",
        interval_minutes=1,
        next_cycle_mmss="1:00",
        next_fire_at=next_fire,
    )

    tpl = build_templates()
    html = tpl.env.get_template("partials/_header_status.html.jinja").render(monitor=evt)

    assert 'data-next-check-at="2026-05-18T10:31:00Z"' in html, (
        f"Expected data-next-check-at attribute in rendered HTML. Got:\n{html}"
    )


def test_header_status_template_renders_empty_data_next_check_at_on_initial_render() -> None:
    """Initial-render VM (next_fire_at_iso='') → attribute present but empty.

    The JS countdown gracefully skips ticking when the attribute is empty,
    so the initial render before the first SSE cycle is safe.
    """
    from fis_monitor.web.templates import build_templates

    vm = build_monitor_vm(
        settings=Settings(interval_minutes=5),
        session=_session(),
        lot_repo=FakeLotRepository(),
        now=_NOW,
    )

    tpl = build_templates()
    html = tpl.env.get_template("partials/_header_status.html.jinja").render(monitor=vm)

    # Attribute must be present (even if empty) for JS to detect the element.
    assert "data-next-check-at" in html, (
        f"data-next-check-at attribute must be present in initial render. Got:\n{html}"
    )
    assert 'data-next-check-at=""' in html, (
        f"data-next-check-at must be empty on initial render. Got:\n{html}"
    )
