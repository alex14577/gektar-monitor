"""FastAPI APIRouter for notification endpoints.

Endpoints:
  GET /notifications — list recent notifications via NotificationsRepository.list_recent()

DI: all dependencies injected via Depends(); routes decoupled from Container and
testable via app.dependency_overrides.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from fis_monitor.domain.interfaces import NotificationsRepository
from fis_monitor.web.deps import get_notifications_repo

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
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
def list_notifications(
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    repo: NotificationsRepository = Depends(get_notifications_repo),
) -> JSONResponse:
    """Return the most recent *limit* notification records."""
    records = repo.list_recent(limit)
    return JSONResponse(
        content=[r.model_dump(mode="json") for r in records],
    )
