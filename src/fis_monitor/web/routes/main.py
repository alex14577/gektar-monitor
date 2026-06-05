"""GET / — главный экран после завершённого онбординга.

SRP: этот роут только компонует контекст из зависимостей и рендерит
feed.html.jinja.  Никакой бизнес-логики здесь нет — каждая «фича»
(DnD-статус, фильтры из cookie, catch-up банер, archive-счётчик) живёт в
своём сервисе и проброшена через ``Depends``.

DIP: всё через Depends — get_dnd_service, get_catchup_dismiss,
get_view_filters_service, get_user_state_repo, get_clock и т.д.
Container в роуте не виден.

MVP-stub, переезжающие в follow-up bd:
  * ``last_cycle`` / ``health`` — отдельная задача (зависит от CyclesRepository
    + HealthService).
  * ``catchup.new_count`` — текущий MVP-показатель: число активных лотов в
    БД (``LotRepository.count_active``).  Точный «сколько появилось с момента
    последнего визита» = отдельный bd когда LotRepository обзаведётся
    ``count_first_seen_since(at)``.

``zones.hot`` / ``zones.today`` / ``archive_count`` рендерятся через
``LotQueryService.search`` + feed_context helpers: фильтры из cookie
сайдбара (``ViewFilters``) транслируются в ``LotFilters`` для SQL и
``only_new`` применяется post-filter.
"""

from __future__ import annotations

import importlib.metadata
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from fis_monitor.domain.interfaces import Clock, LotRepository, UserStateRepository

if TYPE_CHECKING:
    from fis_monitor.container import SessionProbe
from fis_monitor.domain.models import SessionStatus, Settings
from fis_monitor.services.backfill import BackfillService
from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.services.dnd import DndService
from fis_monitor.services.login import LoginService
from fis_monitor.services.lot_query import LotQueryService
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import (
    get_backfill,
    get_catchup_dismiss,
    get_clock,
    get_config_source,
    get_dnd_service,
    get_license_result,
    get_login,
    get_lot_query,
    get_lot_repo,
    get_session_probe,
    get_templates,
    get_user_state_repo,
    get_view_filters_service,
)
from fis_monitor.web.feed_context import (
    _FEED_PAGE_SIZE,
    _view_filters_to_lot_filters,
    build_feed_context,
    lot_passes_only_new,
)
from fis_monitor.web.monitor_vm import build_monitor_vm
from fis_monitor.web.sse_encoder import LotUserViewModel

try:
    _APP_VERSION: str = importlib.metadata.version("fis-monitor")
except importlib.metadata.PackageNotFoundError:  # пакет не установлен (dev-окружение)
    _APP_VERSION = "dev"

router = APIRouter(prefix="", tags=["main"])
logger = logging.getLogger(__name__)

# Cookie key produced by ``ViewFiltersService.serialize()`` — kept in sync with
# the constant used by the /filters/* routes.
_VIEW_FILTERS_COOKIE = "view_filters"


# ---------------------------------------------------------------------------
# Context builders — one helper per template slice (high cohesion)
# ---------------------------------------------------------------------------


def _build_session_context(status: SessionStatus) -> SimpleNamespace:
    """Derive template session vars from SessionProbe.check() result.

    SessionStatus is a simple StrEnum (no expires_at field) — expires_at_hhmm
    is left as empty string until HttpSessionProbe (a4t.8) is implemented.
    """
    return SimpleNamespace(
        expires_soon=(status == SessionStatus.EXPIRING),
        expires_at_hhmm="",  # populated by a4t.8 (HttpSessionProbe)
        expired=(status == SessionStatus.EXPIRED),
    )




def _build_dnd_context(dnd_svc: DndService, now: datetime) -> SimpleNamespace:
    """Render DnD state into template-friendly fields.

    ``until_hhmm`` is the local-time HH:MM string the user sees in the header.
    When DnD is off, ``until_hhmm`` is empty so the template's ``{% if %}``
    guards keep the header clean.
    """
    active = dnd_svc.is_active(now)
    until = dnd_svc.until(now) if active else None
    return SimpleNamespace(
        active=active,
        until_hhmm=until.strftime("%H:%M") if until is not None else "",
    )


def _format_last_visit_human(last_visit: datetime, now: datetime) -> str:
    """Render ``last_visit`` as «N ч назад» / «N мин назад» for the catch-up banner.

    Coarse-grained on purpose: we never show seconds, and we clamp the lower
    bound to «только что» for negative or sub-minute deltas (caused by clock
    skew between server and client, which is irrelevant at this granularity).

    Tz handling: if exactly one of the two datetimes is naive, strip tzinfo
    from the aware one so subtraction works in both directions.  Production
    invariant is "Clock returns UTC-aware, DB stores UTC-aware ISO", so this
    guard exists for symmetry / defensive parity, not as a hot path.
    """
    if last_visit.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif now.tzinfo is None and last_visit.tzinfo is not None:
        last_visit = last_visit.replace(tzinfo=None)
    delta = now - last_visit
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def _build_catchup_context(
    *,
    is_dismissed: bool,
    last_visit: datetime | None,
    new_count: int,
    now: datetime,
) -> SimpleNamespace | None:
    """Return the catch-up banner context, or ``None`` when it must stay hidden.

    Hidden when the user has dismissed the banner for this window, has never
    visited the dashboard before (fresh install), or has nothing new to show.
    """
    if is_dismissed or last_visit is None or new_count <= 0:
        return None
    return SimpleNamespace(
        new_count=new_count,
        last_visit_human=_format_last_visit_human(last_visit, now),
        detail=None,
    )


def _build_license_context(license_result: object, now: datetime) -> SimpleNamespace | None:
    """Build template-friendly namespace from LicenseResult.

    Returns None when expires_at is None (INVALID / missing key) so
    the template can hide the license block entirely.
    """
    from fis_monitor.licensing import LicenseStatus

    expires_at = getattr(license_result, "expires_at", None)
    if expires_at is None:
        return None
    status = getattr(license_result, "status", None)
    expired = status == LicenseStatus.EXPIRED
    days_left = max(0, (expires_at - now.date()).days)
    return SimpleNamespace(expires_at=expires_at, expired=expired, days_left=days_left)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def feed_page(
    request: Request,
    config_source: object = Depends(get_config_source),
    session_probe: SessionProbe = Depends(get_session_probe),
    login: LoginService = Depends(get_login),
    dnd_svc: DndService = Depends(get_dnd_service),
    catchup_svc: CatchupDismissService = Depends(get_catchup_dismiss),
    filters_svc: ViewFiltersService = Depends(get_view_filters_service),
    user_state_repo: UserStateRepository = Depends(get_user_state_repo),
    lot_repo: LotRepository = Depends(get_lot_repo),
    lot_query: LotQueryService = Depends(get_lot_query),
    clock: Clock = Depends(get_clock),
    templates: Jinja2Templates = Depends(get_templates),
    backfill_svc: BackfillService = Depends(get_backfill),
    license_result: object = Depends(get_license_result),
) -> HTMLResponse:
    """Render the main feed page (state=COMPLETED guaranteed by middleware).

    If the request reached this handler, OnboardingGateMiddleware has already
    confirmed that onboarding is COMPLETED — no duplicate gate check needed.

    Wires DnD-status, view-filters cookie, catch-up banner, and scope chips;
    feed zones remain empty until the LotUserDTO ↔ template field mapping is
    completed (separate follow-up bd — see module docstring).
    """
    settings: Settings = config_source.current()  # type: ignore[attr-defined]
    now = clock.now()

    # ── Session state ───────────────────────────────────────────────────
    # SessionProbe.check() in production graph is currently
    # _NotImplementedSessionProbe (bd a4t.8 — HttpSessionProbe deferred).
    # Defensive fallback: NotImplementedError → consult LoginService.
    try:
        raw_status: SessionStatus = session_probe.check()
    except NotImplementedError:
        last = login.status().last_outcome
        raw_status = (
            SessionStatus.ACTIVE
            if last is not None and last.success
            else SessionStatus.EXPIRED
        )

    # ── View filters (cookie-persisted, no DB) ──────────────────────────
    cookie_value = request.cookies.get(_VIEW_FILTERS_COOKIE, "")
    parsed_filters = filters_svc.deserialize(cookie_value) or ViewFilters()

    # ── Catch-up banner ─────────────────────────────────────────────────
    # MVP heuristic for ``new_count``: number of currently active lots.  The
    # exact "new since last visit" requires a per-timestamp count query on the
    # lots table — separate bd (see module docstring).
    last_visit = user_state_repo.last_visit()
    active_count = lot_repo.count_active()

    # ── Feed zones (server-rendered initial paint) ──────────────────────
    # At most _FEED_PAGE_SIZE lots are surfaced inline; lots beyond this fall
    # into archive_count only (separate bd for dedicated COUNT by age range).
    feed_ctx = build_feed_context(
        filters=parsed_filters,
        lot_query=lot_query,
        settings=settings,
        active_lot_count=active_count,
    )
    catchup_ctx = _build_catchup_context(
        is_dismissed=catchup_svc.is_dismissed(now),
        last_visit=last_visit,
        new_count=active_count,
        now=now,
    )

    # ── Assemble template context ───────────────────────────────────────
    ctx = {
        "request": request,
        "settings": settings,
        "session": (session_ctx := _build_session_context(raw_status)),
        "monitor": build_monitor_vm(
            settings=settings,
            session=session_ctx,
            lot_repo=lot_repo,
            now=now,
            awaiting_backfill=not backfill_svc.is_done(),
        ),
        # MVP-stub: lot-feed query — separate bd
        "last_cycle": SimpleNamespace(
            error=False,
            started_at_hhmm="",
            error_short="",
            id=0,
        ),
        **feed_ctx,
        "catchup": catchup_ctx,
        "dnd": _build_dnd_context(dnd_svc, now),
        # MVP-stub: browser tab title — separate bd (lot counter from SSE)
        "title_format": "(0) Монитор гектара",
        "license": _build_license_context(license_result, now),
        "app_version": _APP_VERSION,
    }

    return templates.TemplateResponse(request, "feed.html.jinja", ctx)


# ---------------------------------------------------------------------------
# Load-more endpoint
# ---------------------------------------------------------------------------


@router.get("/feed/more", response_class=HTMLResponse, include_in_schema=False)
def feed_more(
    request: Request,
    cursor: str = Query(..., description="Opaque keyset cursor from previous page"),
    shown: int = Query(0, description="Running count of lots shown before this page"),
    filters_svc: ViewFiltersService = Depends(get_view_filters_service),
    lot_query: LotQueryService = Depends(get_lot_query),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Return the next page of lot cards as an HTML partial.

    Reads the same ``view_filters`` cookie as ``feed_page`` so filters never
    diverge between the initial render and load-more pages.  Uses the shared
    ``_view_filters_to_lot_filters`` adapter (DRY — no filter logic here).

    Applies the same ``only_new`` post-filter via ``lot_passes_only_new``
    (the single source of truth for that predicate).

    ``shown`` is a stateless running counter threaded through the cursor URL by
    each ``#load-more-trigger``.  The response template uses it to update the
    ``#feed-lot-count`` OOB span so the filter-bar counter stays accurate as
    lots accumulate.

    Returns ``partials/_feed_more.html.jinja`` which loops lot cards and,
    when ``next_cursor`` is not None, injects a fresh ``#load-more-trigger``
    div so the user can continue paging.

    Raises:
        HTTPException(422): when the cursor is malformed (``lot_query.search``
            raises ``ValueError``).  Consistent with the project convention
            for invalid client-supplied input (see ``filters.py``).
    """
    # Read view_filters cookie — same logic as feed_page (DRY via shared svc).
    cookie_value = request.cookies.get(_VIEW_FILTERS_COOKIE, "")
    parsed_filters = filters_svc.deserialize(cookie_value) or ViewFilters()

    lot_filters = _view_filters_to_lot_filters(parsed_filters)
    try:
        page = lot_query.search(lot_filters, page_size=_FEED_PAGE_SIZE, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid cursor: {exc}") from exc

    # Apply only_new post-filter (same predicate as _assemble_feed_zones).
    visible: list[LotUserViewModel] = [
        LotUserViewModel(dto)
        for dto in page.items
        if lot_passes_only_new(dto, only_new=parsed_filters.only_new)
    ]

    # shown_total = lots rendered before this page + lots on this page.
    # Threaded through the &shown= param so the server stays stateless.
    shown_total = shown + len(visible)

    ctx = {
        "lots": visible,
        "next_cursor": page.next_cursor,
        "shown_total": shown_total,
    }
    return templates.TemplateResponse(request, "partials/_feed_more.html.jinja", ctx)

# ---------------------------------------------------------------------------
# Lot-count re-sync endpoint (B1, ADR-060 amendment 2026-06-01)
# ---------------------------------------------------------------------------


@router.get("/feed/count", response_class=HTMLResponse, include_in_schema=False)
def feed_count(
    request: Request,
    filters_svc: ViewFiltersService = Depends(get_view_filters_service),
    lot_query: LotQueryService = Depends(get_lot_query),
    lot_repo: LotRepository = Depends(get_lot_repo),
    templates: Jinja2Templates = Depends(get_templates),
    config_source: object = Depends(get_config_source),
    session_probe: object = Depends(get_session_probe),
    login: LoginService = Depends(get_login),
    clock: Clock = Depends(get_clock),
    backfill_svc: BackfillService = Depends(get_backfill),
) -> HTMLResponse:
    """Return the M span (#feed-lot-count) + OOB resync fragments for SSE (re)connect.

    Called by JS on htmx:sseOpen so counters and the header-status widget
    re-sync from the DB/session after a reconnect gap (bd zb3, ADR-060 B1).

    monitor.state reflects the DB/session snapshot at reconnect time
    (awaiting_backfill / active / warning / error).  The transient "checking"
    state is not represented in the VM — a reconnect during a check will show
    "active" until the next SseStatus SSE event arrives.
    """
    settings: Settings = config_source.current()  # type: ignore[attr-defined]
    now = clock.now()
    try:
        raw_status: SessionStatus = session_probe.check()  # type: ignore[attr-defined]
    except NotImplementedError:
        last = login.status().last_outcome
        raw_status = (
            SessionStatus.ACTIVE
            if last is not None and last.success
            else SessionStatus.EXPIRED
        )
    except Exception:
        logger.warning("feed_count.session_probe.failed", exc_info=True)
        last = login.status().last_outcome
        raw_status = (
            SessionStatus.ACTIVE
            if last is not None and last.success
            else SessionStatus.EXPIRED
        )
    session_ctx = _build_session_context(raw_status)
    monitor = build_monitor_vm(
        settings=settings,
        session=session_ctx,
        lot_repo=lot_repo,
        now=now,
        awaiting_backfill=not backfill_svc.is_done(),
    )
    cookie_value = request.cookies.get(_VIEW_FILTERS_COOKIE, "")
    parsed_filters = filters_svc.deserialize(cookie_value) or ViewFilters()
    lot_filters = _view_filters_to_lot_filters(parsed_filters)
    lot_count = lot_query.count(lot_filters)
    active_lot_count = lot_repo.count_active()
    return templates.TemplateResponse(
        request,
        "partials/_feed_count_resync.html.jinja",
        {"lot_count": lot_count, "active_lot_count": active_lot_count, "monitor": monitor},
    )
