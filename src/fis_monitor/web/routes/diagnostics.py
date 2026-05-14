"""FastAPI APIRouter for diagnostics endpoints.

Endpoints:
  POST /diagnostics/build — build a diagnostic zip bundle via DiagnosticsService.

Design (R3-M5 / R4-M10 fail-closed):
  - schema_ok=False → 503 with generic ui_message (no internal details leaked).
  - ok=True         → FileResponse streaming the zip to the caller.

DI: DiagnosticsService injected via Depends(get_diagnostics).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from fis_monitor.services.diagnostics import DiagnosticsService
from fis_monitor.web.deps import get_diagnostics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleanup(path: Path) -> None:
    """Delete *path* silently; used to clean up failed zip attempts."""
    try:
        os.unlink(path)
    except OSError:
        logger.debug("diagnostics.cleanup_skip path=%s", path)


def _cleanup_task(path: Path) -> BackgroundTask:
    """Return a BackgroundTask that deletes the temp zip after streaming."""
    return BackgroundTask(_cleanup, path)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/build", response_model=None)
def build_diagnostics(
    svc: DiagnosticsService = Depends(get_diagnostics),
) -> FileResponse | JSONResponse:
    """Build and return a diagnostic zip bundle.

    Returns:
        FileResponse (zip) on success.
        JSONResponse 503 with ``{"error": ..., "ui_message": ...}`` on schema
        drift (fail-closed, R3-M5 / R4-M10 — no internal details leaked).
    """
    # Write zip to a temp file that persists until FileResponse finishes
    # streaming it to the client.  Using delete=False (Python 3.12+: delete_on_close).
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        suffix=".zip", delete=False
    )
    tmp.close()
    output_path = Path(tmp.name)

    try:
        result = svc.build_zip(output_path)

        if not result.schema_ok:
            # schema drift — fail-closed (R3-M5 / R4-M10): generic message, no leaks
            _cleanup(output_path)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "schema_drift",
                    "ui_message": result.ui_message,
                },
            )

        if not result.ok:
            _cleanup(output_path)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "build_failed",
                    "ui_message": result.ui_message,
                },
            )

        # FileResponse streams the file; background cleanup happens after send.
        return FileResponse(
            path=str(result.output_path),
            media_type="application/zip",
            filename="diagnostic.zip",
            background=_cleanup_task(output_path),
        )
    except Exception:
        _cleanup(output_path)
        raise
