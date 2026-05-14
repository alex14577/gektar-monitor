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

DI providers are defined in ``web/deps.py`` (canonical location):
  - ``get_sse_streamer`` — returns ``c.infra.sse_streamer`` (SseStreamer).
  - ``get_csrf_origin_whitelist`` — returns ``request.app.state.csrf_origin_whitelist``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from fis_monitor.infra.sse.sse_stream import SseStreamer
from fis_monitor.web.deps import get_csrf_origin_whitelist, get_sse_streamer

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

_ORIGIN_LOG_MAX = 80  # chars — defence-in-depth: don't log unbounded user input


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
) -> StreamingResponse | PlainTextResponse:
    """Stream SSE events to the browser.

    Origin check (test #14):
      * No ``Origin`` header → allowed (e.g. direct ``fetch()`` from same
        origin without a cross-origin context, or CLI curl).
      * ``Origin`` header present AND in ``origin_whitelist`` → allowed.
      * ``Origin`` header present AND NOT in whitelist → 421 Misdirected Request.

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

    return StreamingResponse(
        content=streamer.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
