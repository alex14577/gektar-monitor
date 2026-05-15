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
``LotQueryService.search`` + ``_assemble_feed_zones``: фильтры из cookie
сайдбара (``ViewFilters``) транслируются в ``LotFilters`` для SQL и
``only_new``/``only_stars`` применяются post-filter.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from fis_monitor.domain.interfaces import Clock, LotRepository, UserStateRepository
from fis_monitor.domain.models import LotUserDTO, SessionStatus, Settings
from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.services.dnd import DndService
from fis_monitor.services.login import LoginService
from fis_monitor.services.lot_query import LotFilters, LotQueryService
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
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
    get_view_filters_service,
)
from fis_monitor.web.sse_encoder import LotUserViewModel

router = APIRouter(prefix="", tags=["main"])

# Cookie key produced by ``ViewFiltersService.serialize()`` — kept in sync with
# the constant used by the /filters/* routes.
_VIEW_FILTERS_COOKIE = "view_filters"

# Feed zone age thresholds — must match _lot_poster vs _lot_list visual split.
_AGE_HOT_SECS = 3_600          # < 1 hour  → hot zone (poster card)
_AGE_TODAY_SECS = 86_400       # 1 h – 24 h → today zone (list card)

# Single-page feed cap: at most this many active lots are loaded into the
# initial HTML.  Lots beyond this fall into the archive count only.
_FEED_PAGE_SIZE = 200


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


def _build_monitor_context(settings: Settings) -> SimpleNamespace:
    """Build MVP monitor context stub.

    interval_minutes is the only field sourced from real Settings.
    next_cycle_mmss, last_new_human, expires_at_hhmm — real data in future bd.
    """
    return SimpleNamespace(
        state="active",
        interval_minutes=settings.interval_minutes,
        next_cycle_mmss="—",
        last_new_human="—",
        expires_at_hhmm="",
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


def _build_filters_context(filters: ViewFilters) -> SimpleNamespace:
    """Map ``ViewFilters`` onto the field names the sidebar template expects.

    ``area_min`` / ``area_max`` are ``None`` for "no restriction" in the
    domain model; the template binds them straight to ``<input value=...>``
    so we render them as empty strings rather than ``None``.  The ``_label``
    fields are the human-readable echo shown above the range inputs.
    """
    return SimpleNamespace(
        subjects=filters.subjects,
        area_min=filters.area_min if filters.area_min is not None else "",
        area_max=filters.area_max if filters.area_max is not None else "",
        area_min_label=str(filters.area_min) if filters.area_min is not None else "0",
        area_max_label=str(filters.area_max) if filters.area_max is not None else "∞",
        only_new=filters.only_new,
        only_stars=filters.only_stars,
    )


def _filters_are_active(filters: ViewFilters) -> bool:
    """Any non-default selection means the user has narrowed the feed."""
    return bool(
        filters.subjects
        or filters.area_min is not None
        or filters.area_max is not None
        or filters.only_new
        or filters.only_stars
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


def _view_filters_to_lot_filters(vf: ViewFilters) -> LotFilters:
    """Adapt the sidebar ``ViewFilters`` to the storage-level ``LotFilters``.

    ``ViewFilters.subjects`` are RF subject codes serialised as strings (the
    sidebar form sends them as ``<input value="77">``).  ``LotFilters.regions``
    is ``tuple[int, ...]`` and matches against the TEXT ``lots.region`` column
    via ``str(r)`` inside ``LotQueryService``.  Non-numeric / corrupted entries
    are silently dropped — defensive against a stale cookie surviving a schema
    bump.

    ``only_new`` / ``only_stars`` are user-state predicates not available at
    the SQL layer and are applied as an in-memory post-filter in
    ``_assemble_feed_zones``.
    """
    regions: list[int] = []
    for s in vf.subjects:
        try:
            regions.append(int(s))
        except (TypeError, ValueError):
            continue
    return LotFilters(
        regions=tuple(regions),
        area_sqm_min=Decimal(vf.area_min) if vf.area_min is not None else None,
        area_sqm_max=Decimal(vf.area_max) if vf.area_max is not None else None,
    )


def _assemble_feed_zones(
    items: tuple[LotUserDTO, ...],
    *,
    view_filters: ViewFilters,
    subscribed_regions: frozenset[str],
) -> tuple[SimpleNamespace, int]:
    """Group ``LotUserDTO`` items into the template's feed zones.

    Splits by ``age_seconds`` into hot (≤ 1 h) and today (1 h – 24 h);
    everything older counts towards ``archive_count`` but is not rendered
    inline (revealed on demand via the "Показать ещё" button).

    Applies the user-state post-filters (``only_new``, ``only_stars``) that
    the SQL layer cannot express.  Each surfaced lot is wrapped in
    ``LotUserViewModel`` so it can be consumed by the existing partials
    (``_lot_poster.html.jinja`` / ``_lot_list.html.jinja``).
    """
    hot: list[LotUserViewModel] = []
    today: list[LotUserViewModel] = []
    archive_count = 0

    for dto in items:
        if view_filters.only_new and dto.seen_at is not None:
            continue
        if view_filters.only_stars and not dto.starred:
            continue

        if dto.age_seconds < _AGE_HOT_SECS:
            hot.append(LotUserViewModel(dto, subscribed_regions=subscribed_regions))
        elif dto.age_seconds < _AGE_TODAY_SECS:
            today.append(LotUserViewModel(dto, subscribed_regions=subscribed_regions))
        else:
            archive_count += 1

    return SimpleNamespace(hot=tuple(hot), today=tuple(today)), archive_count


def _build_scope_context(settings: Settings) -> SimpleNamespace:
    """Derive sidebar scope chips from configured regions.

    ``macro_regions``: the configured region codes (rendered as chip labels
    by the template).  ``subjects_count``: a count of subjects the user can
    pick from — proxied by configured-region count until the subject catalog
    is wired (separate bd).
    """
    regions = list(settings.regions)
    return SimpleNamespace(
        macro_regions=regions,
        subjects_count=len(regions),
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def feed_page(
    request: Request,
    config_source: object = Depends(get_config_source),
    session_probe: object = Depends(get_session_probe),
    login: LoginService = Depends(get_login),
    dnd_svc: DndService = Depends(get_dnd_service),
    catchup_svc: CatchupDismissService = Depends(get_catchup_dismiss),
    filters_svc: ViewFiltersService = Depends(get_view_filters_service),
    user_state_repo: UserStateRepository = Depends(get_user_state_repo),
    lot_repo: LotRepository = Depends(get_lot_repo),
    lot_query: LotQueryService = Depends(get_lot_query),
    clock: Clock = Depends(get_clock),
    templates: Jinja2Templates = Depends(get_templates),
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
        raw_status: SessionStatus = session_probe.check()  # type: ignore[attr-defined]
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
    # Query active lots applying sidebar filters; group by age into hot /
    # today / archive.  At most _FEED_PAGE_SIZE lots are surfaced inline; if
    # active_count exceeds this, the archive count under-reports — separate
    # bd would add a dedicated COUNT(*) by age range.
    lot_filters = _view_filters_to_lot_filters(parsed_filters)
    page = lot_query.search(lot_filters, page_size=_FEED_PAGE_SIZE)
    subscribed_regions = frozenset(str(r) for r in settings.regions)
    zones, archive_count = _assemble_feed_zones(
        page.items,
        view_filters=parsed_filters,
        subscribed_regions=subscribed_regions,
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
        "session": _build_session_context(raw_status),
        "monitor": _build_monitor_context(settings),
        # MVP-stub: lot-feed query — separate bd
        "last_cycle": SimpleNamespace(
            error=False,
            started_at_hhmm="",
            error_short="",
            id=0,
        ),
        # MVP-stub: health derivation — separate bd
        "health": SimpleNamespace(
            last_cycle_human="—",
            total_lots=active_count,
            last_new_human="—",
        ),
        "zones": zones,
        "catchup": catchup_ctx,
        "scope": _build_scope_context(settings),
        "filters": _build_filters_context(parsed_filters),
        "dnd": _build_dnd_context(dnd_svc, now),
        "archive_count": archive_count,
        "filters_active": _filters_are_active(parsed_filters),
        # MVP-stub: browser tab title — separate bd (lot counter from SSE)
        "title_format": "(0) Монитор гектара",
    }

    return templates.TemplateResponse(request, "feed.html.jinja", ctx)
