"""FastAPI APIRouter for notification endpoints.

Endpoints:
  GET /notifications      — HTML page listing recent notifications.
  GET /notifications.json — JSON API (same data, machine-readable).

PII policy: ``recipient`` is never rendered in plain text.  The HTML page
shows a masked form (``a***@example.com`` for email, ``local`` as-is for
browser/heartbeat channels).  The JSON endpoint intentionally preserves the
full ``recipient`` field for trusted internal consumers; it is not linked from
any public-facing navigation.

DI: all dependencies injected via Depends(); routes decoupled from Container
and testable via app.dependency_overrides.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from fis_monitor.domain.interfaces import Clock, LotRepository, NotificationsRepository
from fis_monitor.domain.models import NotificationRecord, Settings
from fis_monitor.web.deps import (
    get_clock,
    get_config_source,
    get_lot_repo,
    get_notifications_repo,
    get_templates,
)
from fis_monitor.web.monitor_vm import build_monitor_vm

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/notifications", tags=["notifications"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LIMIT_DEFAULT = 100
_LIMIT_MAX = 500

# ---------------------------------------------------------------------------
# PII helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^([^@]{1,2})[^@]*(@.+)$")


def _mask_recipient(recipient: str) -> str:
    """Return a masked representation of *recipient* safe for display.

    Rules:
    - ``local`` (browser/heartbeat sentinel) → returned as-is.
    - Email address → ``a***@example.com`` (first 1-2 chars + *** + domain).
    - Anything else → ``***`` (opaque fallback).
    """
    if recipient == "local":
        return "local"
    m = _EMAIL_RE.match(recipient)
    if m:
        return f"{m.group(1)}***{m.group(2)}"
    return "***"


def _display_timestamp(record: NotificationRecord) -> str:
    """Return the most informative available timestamp as ISO string.

    Priority: sent_at → last_attempt_at → "—".
    """
    ts = record.sent_at or record.last_attempt_at
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def notifications_page(
    request: Request,
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    repo: NotificationsRepository = Depends(get_notifications_repo),
    config_source: object = Depends(get_config_source),
    templates: Jinja2Templates = Depends(get_templates),
    lot_repo: LotRepository = Depends(get_lot_repo),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    """Render the notifications history page."""
    settings: Settings = config_source.current()  # type: ignore[attr-defined]
    records = repo.list_recent(limit)
    rows = [
        {
            "lot_id": r.lot_id,
            "channel": r.channel,
            "status": r.status,
            "attempt_no": r.attempt_no,
            "timestamp": _display_timestamp(r),
            "recipient_masked": _mask_recipient(r.recipient),
        }
        for r in records
    ]
    return templates.TemplateResponse(
        request,
        "notifications.html.jinja",
        {
            "rows": rows,
            "limit": limit,
            # Stubs required by base.html.jinja header/partial rendering.
            "settings": settings,
            "dnd": SimpleNamespace(active=False, until_hhmm=""),
            "session": (_session_ctx := SimpleNamespace(
                expired=False, expires_soon=False, expires_at_hhmm="",
            )),
            "monitor": build_monitor_vm(
                settings=settings,
                session=_session_ctx,
                lot_repo=lot_repo,
                now=clock.now(),
            ),
        },
    )


@router.get(".json")
def list_notifications_json(
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    repo: NotificationsRepository = Depends(get_notifications_repo),
) -> JSONResponse:
    """Return the most recent *limit* notification records as JSON.

    Internal/machine-readable endpoint.  Full ``recipient`` field is included
    intentionally (trusted consumers only — not linked from the UI).
    """
    records = repo.list_recent(limit)
    return JSONResponse(
        content=[r.model_dump(mode="json") for r in records],
    )
