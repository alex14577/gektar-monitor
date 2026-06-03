"""SSE endpoint — GET /events.

Architecture:
  - ``SseStreamer`` (infra/sse/sse_stream.py) handles the sync→async bridge.
  - Origin check is performed HERE before the stream starts (not in middleware):
    ``CsrfHostOriginMiddleware`` skips safe methods (GET), so the SSE endpoint
    must validate Origin itself to prevent DNS-rebinding attacks on long-lived
    connections.  Missing Origin header is allowed (direct browser fetch); an
    *explicit* foreign-origin header is rejected with 421.
  - ``SsePayloadSchema`` redaction is the publisher's responsibility
    (``EventBus.publish`` / ``SseStreamer``).  The route does NOT re-redact.
  - Schema-drift (unknown event type) → event is silently dropped by SseStreamer
    callers; the route has no additional drift handling needed here.
  - View-filter (ADR-052): the ``view_filters`` cookie, if present and valid,
    is parsed at connection time into a per-connection predicate that suppresses
    ``lot.new`` events that do not match the user's active filter.  A missing or
    malformed cookie falls back to pass-through (no suppression).

DI providers are defined in ``web/deps.py`` (canonical location):
  - ``get_sse_streamer`` — returns ``c.infra.sse_streamer`` (SseStreamer).
  - ``get_csrf_origin_whitelist`` — returns ``request.app.state.csrf_origin_whitelist``.
  - ``get_view_filters_service`` — returns a stateless ``ViewFiltersService``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from fis_monitor.domain.interfaces import RegionSubscriptionRepository
from fis_monitor.domain.models import SseEvent
from fis_monitor.infra.sse.sse_stream import SseStreamer
from fis_monitor.services.sse_view_filter import make_sse_membership_filter, make_sse_view_filter
from fis_monitor.services.view_filters import ViewFilters, ViewFiltersService
from fis_monitor.web.deps import (
    get_csrf_origin_whitelist,
    get_region_subscription_repo,
    get_sse_streamer,
    get_view_filters_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

_ORIGIN_LOG_MAX = 80  # chars — defence-in-depth: don't log unbounded user input
_VIEW_FILTERS_COOKIE = "view_filters"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_event_filter(
    request: Request,
    vf_service: ViewFiltersService,
    subscribed_ids: frozenset[int],
) -> Callable[[SseEvent], bool]:
    """Build a per-connection predicate combining membership and view-filter.

    Membership filter always applies (ADR-065).  View-filter from the
    ``view_filters`` cookie is composed on top when present and valid.

    Returns:
        A ``Callable[[SseEvent], bool]``.
    """
    membership = make_sse_membership_filter(subscribed_ids)

    raw_cookie: str | None = request.cookies.get(_VIEW_FILTERS_COOKIE)
    if not raw_cookie:
        return membership

    vf: ViewFilters | None = vf_service.deserialize(raw_cookie)
    if vf is None:
        logger.debug("sse.view_filter.cookie_malformed", extra={"path": request.url.path})
        return membership

    view = make_sse_view_filter(vf)
    return lambda e: membership(e) and view(e)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/events",
    summary="Server-Sent Events stream",
    response_class=StreamingResponse,
    response_model=None,
)
async def sse_events(
    request: Request,
    streamer: Annotated[SseStreamer, Depends(get_sse_streamer)],
    origin_whitelist: Annotated[frozenset[str], Depends(get_csrf_origin_whitelist)],
    vf_service: Annotated[ViewFiltersService, Depends(get_view_filters_service)],
    region_sub_repo: Annotated[RegionSubscriptionRepository, Depends(get_region_subscription_repo)],
) -> StreamingResponse | PlainTextResponse:
    """Stream SSE events to the browser.

    Origin check (test #14):
      * No ``Origin`` header → allowed (e.g. direct ``fetch()`` from same
        origin without a cross-origin context, or CLI curl).
      * ``Origin`` header present AND in ``origin_whitelist`` → allowed.
      * ``Origin`` header present AND NOT in whitelist → 421 Misdirected Request.

    View-filter (ADR-052):
      The ``view_filters`` cookie is read once at connection time and converted
      to a per-connection predicate via ``make_sse_view_filter``.  Events that
      do not pass the predicate are silently suppressed inside ``SseStreamer``.
      A missing or malformed cookie yields the membership-only predicate
      (membership filter always applies).
      Cookie changes while connected require an F5 reload (deferred scope).

    Schema drift:
      Handled inside ``SseStreamer.stream()``.  Unknown event types are dropped
      and logged at ERROR level (``sse.schema_drift``).  The route itself does
      not need additional drift handling.
    """
    origin: str | None = request.headers.get("origin")

    if origin is not None and origin.lower() not in origin_whitelist:
        safe_origin = origin[:_ORIGIN_LOG_MAX]
        logger.warning(
            "sse.origin_rejected",
            extra={"origin": safe_origin, "path": request.url.path},
        )
        return PlainTextResponse(content="421 Misdirected Request", status_code=421)

    subscribed_ids = region_sub_repo.list_subscribed_region_ids()
    event_filter = _build_event_filter(request, vf_service, subscribed_ids)

    return StreamingResponse(
        content=streamer.stream(event_filter=event_filter),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
