"""fake-torgi staging server.

Mirrors the torgi.gov.ru endpoints used by fis_monitor just enough for
manual staging and integration tests.  Zero new dependencies: FastAPI +
Jinja2 + uvicorn are already in prod-deps.

Usage:
    python tools/fake_torgi/server.py --port 8765

Then point the service at it:
    FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor

Admin UI: http://localhost:8765/admin
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote, urlparse

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_LOTS_FILE = _HERE / "lots.json"

_STATUSES = ["Свободен", "Зарезервирован", "Оформляется"]

_SESSIONS: dict[str, float] = {}  # token → created_at (epoch seconds)
_SESSIONS_LOCK = Lock()


def _create_session() -> str:
    """Create a new fake-ESIA session and return its token."""
    import time

    token = secrets.token_urlsafe(16)
    with _SESSIONS_LOCK:
        _SESSIONS[token] = time.time()
    return token


def _is_valid_session(token: str | None) -> bool:
    """Return True iff token exists in the session store."""
    if not token:
        return False
    with _SESSIONS_LOCK:
        return token in _SESSIONS


def _safe_redirect_uri(redirect_uri: str | None, default: str = "/cabinet/") -> str:
    """Accept only relative paths starting with /. Reject scheme/netloc and traversal."""
    if not redirect_uri:
        return default
    # Reject backslashes outright — some legacy browsers treat them as / and
    # collapse `/\evil.com` into a host. urlparse() does not catch this.
    if "\\" in redirect_uri:
        return default
    parsed = urlparse(redirect_uri)
    if parsed.scheme or parsed.netloc:
        return default
    if not parsed.path.startswith("/"):
        return default
    # Reject path traversal — any `..` segment makes the redirect target
    # unpredictable and violates the relative-path contract.
    if ".." in parsed.path.split("/"):
        return default
    path = parsed.path
    if parsed.query:
        path += f"?{parsed.query}"
    return path


app = FastAPI(title="fake-torgi", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _auth_bypass_enabled() -> bool:
    """Return True when FAKE_TORGI_NO_AUTH env-var enables /cabinet/* bypass.

    Used by SessionMiddleware to short-circuit the fake-ESIA flow so monitor_cycle
    can hit /cabinet/* without first running a Playwright headed login. Intended
    for headless-CI / smoke-test environments where running a real browser is
    impractical (e.g. WSL without DISPLAY). Accepted truthy values: 1/true/yes/on.
    Evaluated per-request so it can be toggled at runtime without restart.
    """
    return os.environ.get("FAKE_TORGI_NO_AUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class SessionMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated /cabinet/* requests to fake-ESIA authorize.

    Bypassed entirely when ``FAKE_TORGI_NO_AUTH=1`` is set in the environment
    (see :func:`_auth_bypass_enabled`).
    """

    _PROTECTED_PREFIX = "/cabinet/"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(self._PROTECTED_PREFIX) and not _auth_bypass_enabled():
            token = request.cookies.get("fis_session")
            if not _is_valid_session(token):
                redirect_uri = path
                if request.url.query:
                    redirect_uri += f"?{request.url.query}"
                target = f"/fake-esia/authorize?redirect_uri={quote(redirect_uri, safe='/')}"
                return RedirectResponse(target, status_code=302)
        return await call_next(request)


app.add_middleware(SessionMiddleware)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_lots() -> list[dict[str, Any]]:
    """Load lots from lots.json; return empty list if file absent."""
    if not _LOTS_FILE.exists():
        return []
    return json.loads(_LOTS_FILE.read_text(encoding="utf-8"))


def _save_lots(lots: list[dict[str, Any]]) -> None:
    """Persist lots to lots.json."""
    _LOTS_FILE.write_text(json.dumps(lots, ensure_ascii=False, indent=2), encoding="utf-8")


def _today_str() -> str:
    return datetime.now(tz=UTC).strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# Target-site endpoints (used by fis_monitor parsers)
# ---------------------------------------------------------------------------


@app.get("/cabinet/free-lot", response_class=HTMLResponse)
async def lot_list(
    request: Request,
    region: int = 1,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200, alias="per-page"),
) -> HTMLResponse:
    """Return list-page HTML matching SelectolaxListParser expectations.

    Parser checks: tbody present, tr[data-key], 14+ td[data-col-seq],
    ``.table-paginate__info`` with text «Найдено записей: N из N».

    Pagination semantics mirror the real torgi.gov.ru: ``page`` 1-based,
    ``per-page`` slice size; ``.table-paginate__info`` always shows the
    full total (not the current slice) so the parser-side ``total_count``
    stays consistent across pages. When ``page`` is past the last one the
    response has an empty ``<tbody>`` — that is the stop signal for
    ``PaginatedListFetcher.iterate()`` (real site behaves the same).
    """
    all_lots = _load_lots()
    total = len(all_lots)
    start = (page - 1) * per_page
    end = start + per_page
    page_lots = all_lots[start:end]
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "lots": page_lots,
            "total": total,
            "region": region,
            "page": page,
            "per_page": per_page,
        },
    )


@app.get("/cabinet/free-lot-view", response_class=HTMLResponse)
async def lot_detail(request: Request, id: int = 0) -> HTMLResponse:
    """Return detail-card HTML matching SelectolaxDetailParser expectations.

    Parser checks: .request-declaration__block-main present.
    Returns 404-ish page (still 200, empty block) when lot not found —
    real site behaves the same way for unknown IDs.
    """
    lots = _load_lots()
    lot = next((lo for lo in lots if lo["id"] == id), None)
    if lot is None:
        # Return a minimal valid detail page with empty fields.
        lot = {
            "id": id,
            "cadastral_no": "",
            "area_sqm": 0,
            "region": "",
            "municipality": "",
            "address": "",
            "land_category": "",
            "permitted_use": "",
            "ogv": "",
            "date_create": "",
            "date_update": "",
            "status": "Не найден",
            "lat": "",
            "lon": "",
            "boundaries": "Нет",
        }
    lot = _enrich(lot)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"lot": lot},
    )


def _enrich(lot: dict[str, Any]) -> dict[str, Any]:
    """Fill computed/missing fields so templates never see KeyError."""
    result = dict(lot)
    result.setdefault("address", f"{lot.get('region', '')}, {lot.get('municipality', '')}")
    result.setdefault("boundaries", "Нет")
    result.setdefault("lat", "")
    result.setdefault("lon", "")
    result.setdefault("date_update", "")
    return result


@app.get("/cabinet/", response_class=HTMLResponse)
async def cabinet_stub(request: Request) -> HTMLResponse:
    """Minimal cabinet page; satisfies Playwright wait_for_url('**/cabinet/**')."""
    return templates.TemplateResponse(request, "cabinet_stub.html", {})


@app.get("/fake-esia/authorize", response_class=HTMLResponse)
async def fake_esia_authorize(request: Request, redirect_uri: str = "/cabinet/") -> HTMLResponse:
    """Render fake-ESIA login form."""
    safe = _safe_redirect_uri(redirect_uri)
    return templates.TemplateResponse(
        request, "fake_esia.html", {"redirect_uri": safe}
    )


@app.post("/fake-esia/login")
async def fake_esia_login(redirect_uri: str = Form("/cabinet/")) -> RedirectResponse:
    """Issue a fake-ESIA session cookie and redirect back to the original URL."""
    safe = _safe_redirect_uri(redirect_uri)
    token = _create_session()
    response = RedirectResponse(safe, status_code=302)
    response.set_cookie(
        "fis_session", token, httponly=True, samesite="lax", path="/"
    )
    return response


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_ui(request: Request, msg: str = "") -> HTMLResponse:
    lots = _load_lots()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "lots": lots,
            "message": msg,
            "today": _today_str(),
            "statuses": _STATUSES,
        },
    )


@app.post("/admin/lots")
async def admin_add_lot(
    id: int = Form(...),
    cadastral_no: str = Form(""),
    area_sqm: int = Form(0),
    region: str = Form(""),
    municipality: str = Form(""),
    land_category: str = Form(""),
    permitted_use: str = Form(""),
    ogv: str = Form(""),
    date_create: str = Form(""),
    date_update: str = Form(""),
    status: str = Form("Свободен"),
    lat: str = Form(""),
    lon: str = Form(""),
) -> RedirectResponse:
    """Add a lot via admin form.  PRG pattern: redirect to /admin after POST."""
    lots = _load_lots()
    if any(lo["id"] == id for lo in lots):
        return RedirectResponse(f"/admin?msg=ID+{id}+already+exists", status_code=303)
    lots.append(
        {
            "id": id,
            "cadastral_no": cadastral_no,
            "area_sqm": area_sqm,
            "region": region,
            "municipality": municipality,
            "land_category": land_category,
            "permitted_use": permitted_use,
            "ogv": ogv,
            "date_create": date_create or _today_str(),
            "date_update": date_update,
            "status": status,
            "lat": lat,
            "lon": lon,
            "boundaries": "Есть" if lat else "Нет",
            "address": f"{region}, {municipality}",
        }
    )
    _save_lots(lots)
    return RedirectResponse(f"/admin?msg=Lot+{id}+added", status_code=303)


@app.post("/admin/lots/{lot_id}/delete")
async def admin_delete_lot(lot_id: int) -> RedirectResponse:
    """Delete a lot by ID.  PRG redirect to /admin."""
    lots = _load_lots()
    lots = [lo for lo in lots if lo["id"] != lot_id]
    _save_lots(lots)
    return RedirectResponse(f"/admin?msg=Lot+{lot_id}+deleted", status_code=303)


@app.post("/admin/lots/{lot_id}/status")
async def admin_change_status(lot_id: int, status: str = Form(...)) -> RedirectResponse:
    """Change lot status.  PRG redirect to /admin."""
    lots = _load_lots()
    for lot in lots:
        if lot["id"] == lot_id:
            lot["status"] = status
            break
    _save_lots(lots)
    return RedirectResponse(f"/admin?msg=Lot+{lot_id}+status+updated", status_code=303)


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------


@app.get("/status")
async def status() -> dict[str, Any]:
    """Machine-readable health check: lot count + server identity."""
    lots = _load_lots()
    return {"ok": True, "lots": len(lots), "server": "fake-torgi"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fake-torgi staging server")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if _auth_bypass_enabled():
        print(
            "fake-torgi: FAKE_TORGI_NO_AUTH=1 — /cabinet/* auth bypass ENABLED "
            "(monitor_cycle will hit endpoints without fake-ESIA login).",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port)
