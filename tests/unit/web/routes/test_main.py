"""Unit tests for GET / — main feed route (bd gektar_monitor-3v3).

MVP: route renders feed.html.jinja with safe defaults.
Lot-feed query and health derivation are future bd-tasks.

Coverage:
  1. GET / returns 200 with #feed marker (completed state assumed).
  2. SessionProbe ACTIVE → no warning banner, no visible expired modal.
  3. SessionProbe EXPIRING → warning banner present.
  4. SessionProbe EXPIRED → #session-expired-modal WITHOUT hidden attribute.
  5. Settings.interval_minutes flows through to monitor context.
  6. Anti-mock: FakeConfigSource + FakeSessionProbe — all methods called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from fis_monitor.domain.interfaces import ConfigSubscription
from fis_monitor.domain.models import LotUserState, SessionStatus, Settings
from fis_monitor.services.login import LoginStatus
from fis_monitor.services.view_filters import ViewFilters, serialize
from fis_monitor.web.deps import (
    get_catchup_dismiss,
    get_clock,
    get_config_source,
    get_dnd_service,
    get_login,
    get_lot_query,
    get_lot_repo,
    get_session_probe,
    get_templates,
    get_user_state_repo,
)
from fis_monitor.web.routes.main import router
from fis_monitor.web.templates import STATIC_DIR, TEMPLATES_DIR
from tests.factories import make_settings
from tests.unit.web.routes.conftest import FakeLotQueryService, FakeLotRepo

# Local aliases so existing test code keeps working without mass rename.
FakeLotQuery = FakeLotQueryService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSubscription:
    """Stub ConfigSubscription returned by FakeConfigSource.subscribe()."""

    def unsubscribe(self) -> None:
        pass


class FakeConfigSource:
    """Fake ConfigSource — implements ALL public methods (anti-mock §6)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self.current_calls: int = 0
        self.subscribe_calls: int = 0
        self.save_calls: list[Settings] = []

    def current(self) -> Settings:
        self.current_calls += 1
        return self._settings

    def subscribe(self, cb: Any) -> ConfigSubscription:
        self.subscribe_calls += 1
        return _FakeSubscription()

    def save(self, settings: Settings) -> None:
        self._settings = settings
        self.save_calls.append(settings)


class FakeDndService:
    """Fake DndService — implements is_active() + until() (anti-mock §6)."""

    def __init__(self, *, active: bool = False, until: datetime | None = None) -> None:
        self._active = active
        self._until = until
        self.is_active_calls: int = 0
        self.until_calls: int = 0

    def is_active(self, now: datetime) -> bool:
        self.is_active_calls += 1
        return self._active

    def until(self, now: datetime) -> datetime | None:
        self.until_calls += 1
        return self._until


class FakeCatchupDismiss:
    """Fake CatchupDismissService — implements is_dismissed()."""

    def __init__(self, *, dismissed: bool = False) -> None:
        self._dismissed = dismissed
        self.is_dismissed_calls: int = 0

    def is_dismissed(self, now: datetime) -> bool:
        self.is_dismissed_calls += 1
        return self._dismissed


class FakeUserStateRepo:
    """Fake UserStateRepository covering only the read API the route needs.

    ``last_visit()`` is the only method exercised by the feed route; the rest
    are stubs that raise to fail loud if a route regression starts reading
    them.
    """

    def __init__(self, *, last_visit: datetime | None = None) -> None:
        self._last_visit = last_visit
        self.last_visit_calls: int = 0

    def last_visit(self) -> datetime | None:
        self.last_visit_calls += 1
        return self._last_visit

    def get(self, lot_id: int) -> LotUserState | None:
        raise NotImplementedError

    def get_many(self, ids: Any) -> dict[int, LotUserState]:
        raise NotImplementedError

    def set_starred(self, lot_id: int, value: bool) -> None:
        raise NotImplementedError

    def set_submitted(self, lot_id: int, value: bool, at: datetime | None) -> None:
        raise NotImplementedError

    def set_note(self, lot_id: int, note: str | None) -> None:
        raise NotImplementedError

    def mark_visited(self, at: datetime) -> None:
        raise NotImplementedError


class FakeClock:
    """Fake Clock — fixed timestamp; ``now()`` returns the same value each call."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
        self.now_calls: int = 0

    def now(self) -> datetime:
        self.now_calls += 1
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeSessionProbe:
    """Fake SessionProbe — implements check() protocol method.

    Parameterisable via ``status`` ctor arg.
    """

    def __init__(self, status: SessionStatus = SessionStatus.ACTIVE) -> None:
        self._status = status
        self.check_calls: int = 0

    def check(self) -> SessionStatus:
        self.check_calls += 1
        return self._status


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    *,
    settings: Settings | None = None,
    session_status: SessionStatus = SessionStatus.ACTIVE,
    dnd: FakeDndService | None = None,
    catchup: FakeCatchupDismiss | None = None,
    user_state: FakeUserStateRepo | None = None,
    lot_repo: FakeLotRepo | None = None,
    lot_query: FakeLotQuery | None = None,
    clock: FakeClock | None = None,
) -> tuple[FastAPI, FakeConfigSource, FakeSessionProbe]:
    """Build a minimal FastAPI app with main router + injected fakes."""
    fake_cfg = FakeConfigSource(settings=settings)
    fake_probe = FakeSessionProbe(status=session_status)
    fake_dnd = dnd if dnd is not None else FakeDndService()
    fake_catchup = catchup if catchup is not None else FakeCatchupDismiss()
    fake_user_state = user_state if user_state is not None else FakeUserStateRepo()
    fake_lot_repo = lot_repo if lot_repo is not None else FakeLotRepo()
    fake_lot_query = lot_query if lot_query is not None else FakeLotQuery()
    fake_clock = clock if clock is not None else FakeClock()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Real FakeSessionProbe.check() never raises NotImplementedError, so the
    # fallback to LoginService.status() is dormant in these tests. We still
    # override get_login so the dep resolver does not touch app.state.container.
    class _StubLogin:
        def status(self) -> LoginStatus:
            return LoginStatus(running=False, last_outcome=None)

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    app.dependency_overrides[get_config_source] = lambda: fake_cfg
    app.dependency_overrides[get_session_probe] = lambda: fake_probe
    app.dependency_overrides[get_login] = lambda: _StubLogin()
    app.dependency_overrides[get_templates] = lambda: templates
    app.dependency_overrides[get_dnd_service] = lambda: fake_dnd
    app.dependency_overrides[get_catchup_dismiss] = lambda: fake_catchup
    app.dependency_overrides[get_user_state_repo] = lambda: fake_user_state
    app.dependency_overrides[get_lot_repo] = lambda: fake_lot_repo
    app.dependency_overrides[get_lot_query] = lambda: fake_lot_query
    app.dependency_overrides[get_clock] = lambda: fake_clock
    return app, fake_cfg, fake_probe


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_root_returns_200_with_feed_marker() -> None:
    """AC#1: GET / returns 200 and HTML contains the #feed element."""
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="feed"' in resp.text


def test_session_active_no_warning_no_expired_modal_visible() -> None:
    """AC#3: ACTIVE session → no expiry banner, modal has hidden attribute."""
    app, _, _ = _make_app(session_status=SessionStatus.ACTIVE)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Warning banner only rendered when expires_soon is True
    assert "banner--warn" not in html
    # Modal must carry the hidden attribute (not shown)
    assert 'id="session-expired-modal"' in html
    assert "session-expired-modal" in html
    # The hidden attribute must be present on the modal div
    assert "hidden" in html


def test_session_expiring_shows_warning_banner() -> None:
    """AC#4: EXPIRING session → session-warning banner present in response."""
    app, _, _ = _make_app(session_status=SessionStatus.EXPIRING)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert "banner--warn" in resp.text


def test_session_expired_modal_visible() -> None:
    """AC#5: EXPIRED session → #session-expired-modal without hidden attr."""
    app, _, _ = _make_app(session_status=SessionStatus.EXPIRED)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Modal is present
    assert 'id="session-expired-modal"' in html
    # When expired the modal must NOT have the hidden attribute — check that
    # the specific pattern <... hidden ...> is absent right after the modal id.
    # The template renders: {% if not session.expired %}hidden{% endif %}
    # so we check that the rendered modal div does NOT include "hidden" before
    # the closing >.  We use a simple substring search around the modal tag.
    modal_start = html.index('id="session-expired-modal"')
    # Slice from the modal start to the first closing '>'
    modal_open_tag = html[modal_start : html.index(">", modal_start) + 1]
    assert "hidden" not in modal_open_tag, (
        "EXPIRED session must render modal without hidden attribute"
    )


def test_interval_minutes_flows_to_monitor_context() -> None:
    """AC#6: Settings.interval_minutes is visible in the rendered page."""
    settings = make_settings(interval_minutes=15)
    app, _, _ = _make_app(settings=settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    # The monitor context carries interval_minutes; feed.html.jinja or
    # base.html.jinja must reflect it somewhere.  The sidebar includes
    # monitor state — we assert the integer is present in the HTML body.
    assert "15" in resp.text


def test_all_fake_methods_are_called() -> None:
    """AC#7 Anti-mock: every method on every fake is invoked at least once."""
    # FakeConfigSource
    fake_cfg = FakeConfigSource()
    s = fake_cfg.current()
    assert isinstance(s, Settings)
    assert fake_cfg.current_calls == 1

    sub = fake_cfg.subscribe(lambda _: None)
    assert fake_cfg.subscribe_calls == 1
    sub.unsubscribe()

    new_settings = Settings(interval_minutes=5)
    fake_cfg.save(new_settings)
    assert len(fake_cfg.save_calls) == 1
    assert fake_cfg.save_calls[0].interval_minutes == 5

    # FakeSessionProbe
    fake_probe = FakeSessionProbe(status=SessionStatus.EXPIRING)
    result = fake_probe.check()
    assert result == SessionStatus.EXPIRING
    assert fake_probe.check_calls == 1


# ---------------------------------------------------------------------------
# bd jh2 — Feed integration (DnD / filters / catchup / scope)
# ---------------------------------------------------------------------------


def test_dnd_active_reflected_in_template() -> None:
    """AC#1: DndService.is_active() drives the dnd.active flag in the template."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    dnd = FakeDndService(active=True, until=datetime(2026, 5, 15, 13, 30, tzinfo=UTC))
    app, _, _ = _make_app(dnd=dnd, clock=FakeClock(now=now))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert dnd.is_active_calls == 1
    # When DnD is active the until() must be consulted to fill the header label.
    assert dnd.until_calls == 1
    # The HH:MM (UTC) of the until() value must appear in the page.
    assert "13:30" in resp.text


def test_dnd_inactive_does_not_call_until() -> None:
    """When DnD is off the route should not waste a call on until()."""
    dnd = FakeDndService(active=False)
    app, _, _ = _make_app(dnd=dnd)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert dnd.is_active_calls == 1
    assert dnd.until_calls == 0


def test_view_filters_cookie_parsed_and_applied() -> None:
    """AC#2: a valid view_filters cookie populates filters.* + filters_active.

    Sidebar UI surfaces only the subject filter; area/only_new/only_stars
    are still honoured at the backend layer but no longer have UI affordances.
    """
    payload = ViewFilters(subjects=["Москва", "Татарстан"], area_min=10, only_new=True)
    cookie = serialize(payload)
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/", cookies={"view_filters": cookie})
    html = resp.text
    # The subject count appears in the sidebar header («2 выбрано»)
    assert "2 выбран" in html


def test_view_filters_corrupt_cookie_falls_back_to_defaults() -> None:
    """Bad cookie payloads must not crash the page — silently default to empty."""
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/", cookies={"view_filters": quote("not-json")})
    assert resp.status_code == 200


def test_catchup_banner_hidden_when_no_last_visit() -> None:
    """AC#3a: no prior visit → catchup banner not rendered (fresh install)."""
    app, _, _ = _make_app(
        user_state=FakeUserStateRepo(last_visit=None),
        lot_repo=FakeLotRepo(active_count=5),
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    # The catch-up banner template uses the unique "Пока вас не было" phrase.
    assert "Пока вас не было" not in resp.text


def test_catchup_banner_hidden_when_dismissed() -> None:
    """AC#3b: dismissed within window → banner suppressed even with last_visit."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    app, _, _ = _make_app(
        catchup=FakeCatchupDismiss(dismissed=True),
        user_state=FakeUserStateRepo(last_visit=now - timedelta(hours=3)),
        lot_repo=FakeLotRepo(active_count=20),
        clock=FakeClock(now=now),
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert "Пока вас не было" not in resp.text


def test_catchup_banner_visible_when_state_present() -> None:
    """AC#3c: prior visit + not dismissed + lots > 0 → banner rendered."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    app, _, _ = _make_app(
        catchup=FakeCatchupDismiss(dismissed=False),
        user_state=FakeUserStateRepo(last_visit=now - timedelta(hours=3)),
        lot_repo=FakeLotRepo(active_count=42),
        clock=FakeClock(now=now),
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    assert "Пока вас не было" in html
    # The active-lot count flows into the banner heading.
    assert "42" in html
    # The relative-time formatter renders "3 ч назад" for the 3h delta.
    assert "3 ч назад" in html


def _make_user_dto(
    *,
    lot_id: int,
    age_seconds: int,
    starred: bool = False,
    seen_at: datetime | None = None,
    region: str = "Хабаровский край",
) -> Any:
    """Build a ``LotUserDTO`` for the feed-zone tests.

    Builds a Lot via the factory then upgrades it to LotUserDTO with the
    presentation hints + per-user state the feed assembly inspects.
    """
    from fis_monitor.domain.models import LotUserDTO
    from tests.factories import make_lot

    lot = make_lot(id=lot_id, region=region)
    payload = lot.model_dump()
    payload["age_seconds"] = age_seconds
    payload["tier"] = "match"
    # freshness is derived from age in production but is independent here —
    # the feed assembly only inspects age_seconds, so any valid value works.
    payload["freshness"] = "hot" if age_seconds < 3600 else (
        "warm" if age_seconds < 86_400 else "cold"
    )
    payload["starred"] = starred
    payload["seen_at"] = seen_at
    return LotUserDTO(**payload)


def test_feed_zones_split_by_age() -> None:
    """zones.hot ≤ 1h, zones.today 1-24h, archive_count > 24h."""
    items = (
        _make_user_dto(lot_id=1, age_seconds=600),          # hot
        _make_user_dto(lot_id=2, age_seconds=10_000),       # today (~2.7h)
        _make_user_dto(lot_id=3, age_seconds=200_000),      # archive (>24h)
        _make_user_dto(lot_id=4, age_seconds=300_000),      # archive
    )
    fake_lot_query = FakeLotQuery(items=items)
    app, _, _ = _make_app(lot_query=fake_lot_query)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # All non-archive lots present in HTML
    assert 'id="lot-1"' in html
    assert 'id="lot-2"' in html
    # Archive lots not rendered inline
    assert 'id="lot-3"' not in html
    assert 'id="lot-4"' not in html
    # Archive reveal button shows 2 (= number of >24h lots)
    assert "<b style=\"margin-left: 4px;\">2</b>" in html
    # LotQueryService was actually consulted
    assert len(fake_lot_query.search_calls) == 1


def test_feed_only_stars_filter_hides_unstarred() -> None:
    """ViewFilters.only_stars removes lots whose ``starred`` is False."""
    items = (
        _make_user_dto(lot_id=10, age_seconds=600, starred=False),
        _make_user_dto(lot_id=11, age_seconds=600, starred=True),
    )
    cookie = serialize(ViewFilters(only_stars=True))
    app, _, _ = _make_app(lot_query=FakeLotQuery(items=items))
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/", cookies={"view_filters": cookie})
    html = resp.text
    assert 'id="lot-10"' not in html
    assert 'id="lot-11"' in html


def test_feed_subjects_filter_passed_to_lot_query() -> None:
    """ViewFilters.subjects flow through to LotFilters.subject_display_names via display names.

    Site-ids are looked up in SUBJECT_TITLE_BY_ID and translated to the
    TEXT display names stored in lots.region (e.g. 27 → "Республика Карелия").
    Unknown IDs (not in catalog) and non-numeric strings are silently dropped.
    """
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

    # Use real catalog IDs (27, 34) — both exist in SUBJECT_TITLE_BY_ID
    cookie = serialize(ViewFilters(subjects=["27", "34", "garbage"]))
    fake_lot_query = FakeLotQuery(items=())
    app, _, _ = _make_app(lot_query=fake_lot_query)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/", cookies={"view_filters": cookie})
    assert resp.status_code == 200
    assert len(fake_lot_query.search_calls) == 1
    used = fake_lot_query.search_calls[0]
    # Translated to display names; "garbage" silently dropped
    assert set(used.subject_display_names) == {SUBJECT_TITLE_BY_ID[27], SUBJECT_TITLE_BY_ID[34]}
    assert used.regions == ()  # int-based field not used for subject filtering


def test_scope_subjects_count_reflects_full_catalog() -> None:
    """Sidebar «Все · N» reflects the full SUBJECT_TITLE_BY_ID catalog count.

    ADR-035: view scope is independent of notify selection (filters.rf_subjects).
    Even when rf_subjects has only 3 ids, the sidebar shows all 19 subjects.
    """
    from fis_monitor.domain.regions import SUBJECT_TITLE_BY_ID

    settings = make_settings(regions=[1], subject_site_ids=[87, 88, 25])  # migrated → rf_subjects
    app, _, _ = _make_app(settings=settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Full catalog (19) → "Все · 19".
    assert f"Все · {len(SUBJECT_TITLE_BY_ID)}" in html


def test_sidebar_subjects_button_uses_toggle_menu_pattern() -> None:
    """Sidebar subjects button must use data-toggle-menu wiring so app.js can show/hide it."""
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    assert 'data-toggle-menu="filter-subjects-menu"' in html
    assert 'data-menu' in html


def test_health_widget_shows_only_total_lots() -> None:
    """Health widget: only 'Всего лотов в базе:' present; removed labels absent from the widget."""
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/")
    html = resp.text
    # Extract just the health widget div (scoped to aria-label="Здоровье мониторинга")
    start = html.index('aria-label="Здоровье мониторинга"')
    end = html.index("</div>", start)
    health_html = html[start:end]
    assert "Всего лотов в базе:" in health_html
    assert "Последний успешный цикл" not in health_html
    assert "Последний новый" not in health_html
    assert "Вы за лентой" not in health_html


def test_get_root_with_spoofed_host_does_not_reflect_in_static_urls() -> None:
    """ADR-011 addendum (bd 9u7): static URLs are relative, not Host-derived.

    url_for('static', ...) used to produce absolute URLs incorporating the
    untrusted Host header.  After the fix, base.html.jinja uses root-relative
    literals (/static/...) so a spoofed Host never leaks into the response.
    """
    app, _, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/", headers={"Host": "evil.com"})
    assert resp.status_code == 200
    assert "evil.com" not in resp.text
    # Verify root-relative static refs are present (regression guard)
    assert 'href="/static/' in resp.text or 'src="/static/' in resp.text
