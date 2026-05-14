"""GET / — главный экран после завершённого онбординга.

SRP: этот роут только компонует контекст из зависимостей и рендерит
feed.html.jinja.  Никакой бизнес-логики здесь нет.

DIP: всё через Depends — get_config_source, get_session_probe, get_templates.
Container в роуте не виден.

MVP-stub: lot-feed и health деривация — отдельные bd-таски.
Реальный LotQueryService.feed_snapshot() и HealthService подключатся
позже без изменения этого модуля (OCP).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from fis_monitor.domain.models import SessionStatus, Settings
from fis_monitor.services.login import LoginService
from fis_monitor.web.deps import (
    get_config_source,
    get_login,
    get_session_probe,
    get_templates,
)

router = APIRouter(prefix="", tags=["main"])


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


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def feed_page(
    request: Request,
    config_source: object = Depends(get_config_source),
    session_probe: object = Depends(get_session_probe),
    login: LoginService = Depends(get_login),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Render the main feed page (state=COMPLETED guaranteed by middleware).

    If the request reached this handler, OnboardingGateMiddleware has already
    confirmed that onboarding is COMPLETED — no duplicate gate check needed.

    Context variables marked «MVP-stub» will be replaced by real service calls
    in future bd-tasks (LotQueryService, HealthService, DndService).
    """
    settings: Settings = config_source.current()  # type: ignore[attr-defined]
    # SessionProbe.check() in production graph is currently
    # _NotImplementedSessionProbe (bd a4t.8 — HttpSessionProbe deferred).
    # Defensive fallback: NotImplementedError → consult LoginService.
    # If the user has just completed a successful headed login in this process,
    # LoginService.status().last_outcome.success is True — treat session as
    # ACTIVE so the "Сессия истекла" modal disappears after the post-login
    # window.location.reload() in auth.js. Otherwise default to EXPIRED so a
    # fresh post-onboarding instance with no cookies surfaces the login CTA.
    try:
        raw_status: SessionStatus = session_probe.check()  # type: ignore[attr-defined]
    except NotImplementedError:
        last = login.status().last_outcome
        raw_status = (
            SessionStatus.ACTIVE
            if last is not None and last.success
            else SessionStatus.EXPIRED
        )

    session = _build_session_context(raw_status)
    monitor = _build_monitor_context(settings)

    ctx = {
        "request": request,
        "settings": settings,
        "session": session,
        "monitor": monitor,
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
            total_lots=0,
            last_new_human="—",
        ),
        # MVP-stub: feed zones — separate bd (LotQueryService.feed_snapshot())
        "zones": SimpleNamespace(hot=[], today=[]),
        # MVP-stub: catch-up banner — separate bd
        "catchup": None,
        # MVP-stub: geo scope from Settings — separate bd
        "scope": SimpleNamespace(macro_regions=[], subjects_count=0),
        # MVP-stub: view filters — separate bd
        "filters": SimpleNamespace(
            subjects=None,
            area_min=0,
            area_max=0,
            area_min_label="0",
            area_max_label="0",
            only_new=False,
            only_stars=False,
        ),
        # MVP-stub: Do-Not-Disturb — separate bd
        "dnd": SimpleNamespace(active=False, until_hhmm=""),
        # MVP-stub: archive count — separate bd
        "archive_count": 0,
        # MVP-stub: active filter flag — separate bd
        "filters_active": False,
        # MVP-stub: browser tab title — separate bd (lot counter from SSE)
        "title_format": "(0) Монитор гектара",
    }

    return templates.TemplateResponse(request, "feed.html.jinja", ctx)
