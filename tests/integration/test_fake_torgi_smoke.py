"""Smoke tests for tools/fake_torgi/server.py (Layer 5 — e2e tooling).

Tests that the fake-torgi server:
- starts (FastAPI app is importable and valid)
- serves 200 on the main endpoints via httpx.AsyncClient
- returns HTML that the real parsers can parse without raising

Invariants covered:
- GET /cabinet/free-lot returns HTML with <tbody>  (SelectolaxListParser contract)
- GET /cabinet/free-lot-view returns HTML with .request-declaration__block-main
  (SelectolaxDetailParser contract)
- GET /admin returns 200 (admin UI available)
- GET /status returns {"ok": true}
- POST /admin/lots + redirect + lot appears in list response + parseable

NOT covered here: headed Playwright, real network, real SMTP.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# tools/ is NOT installed as a package — add repo root to sys.path for import.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tools.fake_torgi.server as srv_module  # noqa: E402
from tools.fake_torgi.server import app  # noqa: E402


def _make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _make_authed_client() -> AsyncClient:
    """Return a client pre-seeded with a valid fake-ESIA session cookie."""
    token = srv_module._create_session()
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"fis_session": token},
    )


def _patch_lots(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Point srv_module._LOTS_FILE at *path* for this test."""
    monkeypatch.setattr(srv_module, "_LOTS_FILE", path)


# ---------------------------------------------------------------------------
# Endpoint smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["server"] == "fake-torgi"
    assert data["lots"] == 0


@pytest.mark.asyncio
async def test_list_endpoint_returns_200_with_tbody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        resp = await c.get("/cabinet/free-lot", params={"region": 1})
    assert resp.status_code == 200
    assert "<tbody>" in resp.text


@pytest.mark.asyncio
async def test_list_endpoint_parseable_by_list_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty lots.json -> SelectolaxListParser returns [] without ParseBugError."""
    from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        resp = await c.get("/cabinet/free-lot", params={"region": 1})
    rows = SelectolaxListParser().parse(resp.text).rows
    assert rows == []


@pytest.mark.asyncio
async def test_detail_endpoint_returns_200_with_main_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        resp = await c.get("/cabinet/free-lot-view", params={"id": 9999})
    assert resp.status_code == 200
    assert "request-declaration__block-main" in resp.text


@pytest.mark.asyncio
async def test_detail_endpoint_parseable_by_detail_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown lot ID -> SelectolaxDetailParser returns ParsedDetail without raising."""
    from fis_monitor.infra.parsers.detail_parser import SelectolaxDetailParser

    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        resp = await c.get("/cabinet/free-lot-view", params={"id": 42})
    detail = SelectolaxDetailParser().parse(resp.text)
    assert detail is not None


@pytest.mark.asyncio
async def test_admin_ui_returns_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get("/admin")
    assert resp.status_code == 200
    assert "fake-torgi admin" in resp.text


# ---------------------------------------------------------------------------
# Admin CRUD flow
# ---------------------------------------------------------------------------


_SAMPLE_LOT_FORM = {
    "id": "2002",
    "cadastral_no": "14:29:040004:999",
    "area_sqm": "3998",
    "region": "Республика Саха (Якутия)",
    "municipality": "Усть-Алданский улус",
    "land_category": "Земли сельскохозяйственного назначения",
    "permitted_use": "сельскохозяйственное использование",
    "ogv": "АДМИНИСТРАЦИЯ МР",
    "date_create": "15.12.2021",
    "date_update": "23.01.2024",
    "status": "Свободен",
    "lat": "",
    "lon": "",
}


@pytest.mark.asyncio
async def test_add_lot_then_appears_in_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/lots -> PRG redirect -> lot ID visible in list HTML."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        resp = await c.post("/admin/lots", data=_SAMPLE_LOT_FORM, follow_redirects=False)
        assert resp.status_code == 303
        assert "/admin" in resp.headers["location"]

        list_resp = await c.get("/cabinet/free-lot", params={"region": 1})
    assert "2002" in list_resp.text
    assert "14:29:040004:999" in list_resp.text


@pytest.mark.asyncio
async def test_add_lot_parseable_by_list_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lot added via admin -> SelectolaxListParser parses it correctly."""
    from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        await c.post("/admin/lots", data=_SAMPLE_LOT_FORM, follow_redirects=False)
        list_resp = await c.get("/cabinet/free-lot", params={"region": 1})

    rows = SelectolaxListParser().parse(list_resp.text).rows
    assert len(rows) == 1
    row = rows[0]
    assert row.id == 2002
    assert row.cadastral_no == "14:29:040004:999"
    assert row.area_sqm == 3998
    assert row.status == "Свободен"


@pytest.mark.asyncio
async def test_delete_lot_removes_from_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/lots/{id}/delete removes the lot; list HTML no longer contains ID."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_authed_client() as c:
        await c.post("/admin/lots", data=_SAMPLE_LOT_FORM, follow_redirects=False)
        del_resp = await c.post("/admin/lots/2002/delete", follow_redirects=False)
        assert del_resp.status_code == 303
        list_resp = await c.get("/cabinet/free-lot", params={"region": 1})
    assert "2002" not in list_resp.text


@pytest.mark.asyncio
async def test_change_status_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /admin/lots/{id}/status updates the stored status field."""
    lots_path = tmp_path / "lots.json"
    _patch_lots(monkeypatch, lots_path)
    async with _make_client() as c:
        await c.post("/admin/lots", data=_SAMPLE_LOT_FORM, follow_redirects=False)
        status_resp = await c.post(
            "/admin/lots/2002/status",
            data={"status": "Зарезервирован"},
            follow_redirects=False,
        )
        assert status_resp.status_code == 303

    lots = json.loads(lots_path.read_text())
    lot = next(lo for lo in lots if lo["id"] == 2002)
    assert lot["status"] == "Зарезервирован"


# ---------------------------------------------------------------------------
# fake-ESIA / session flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cabinet_redirects_without_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /cabinet/ without session cookie -> 302 to /fake-esia/authorize."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get("/cabinet/", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/fake-esia/authorize" in location
    assert "redirect_uri" in location


@pytest.mark.asyncio
async def test_authorize_renders_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /fake-esia/authorize -> 200 with login form."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get("/fake-esia/authorize", params={"redirect_uri": "/cabinet/"})
    assert resp.status_code == 200
    assert 'action="/fake-esia/login"' in resp.text
    assert 'id="fake-esia-login-btn"' in resp.text


@pytest.mark.asyncio
async def test_login_sets_cookie_and_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /fake-esia/login -> 302 with Set-Cookie fis_session and Location=/cabinet/."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.post(
            "/fake-esia/login",
            data={"redirect_uri": "/cabinet/"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "fis_session=" in resp.headers.get("set-cookie", "")
    assert resp.headers["location"] == "/cabinet/"


@pytest.mark.asyncio
async def test_cabinet_200_with_valid_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After login, GET /cabinet/ with session cookie -> 200."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        await c.post(
            "/fake-esia/login",
            data={"redirect_uri": "/cabinet/"},
            follow_redirects=False,
        )
        # httpx stores cookies automatically in the client jar
        resp = await c.get("/cabinet/", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cabinet_redirects_with_invalid_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /cabinet/ with bogus cookie -> 302 (session not valid)."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get(
            "/cabinet/",
            follow_redirects=False,
            headers={"Cookie": "fis_session=bogus"},
        )
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_status_unaffected_by_middleware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status without cookie -> 200 (middleware must not touch non-cabinet routes)."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.get("/status", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_open_redirect_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /fake-esia/login with absolute external redirect_uri -> falls back to /cabinet/."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.post(
            "/fake-esia/login",
            data={"redirect_uri": "http://evil.com/x"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/cabinet/"


@pytest.mark.asyncio
async def test_cabinet_bypass_via_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAKE_TORGI_NO_AUTH=1 disables SessionMiddleware → /cabinet/* responds 200 without cookie."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    monkeypatch.setenv("FAKE_TORGI_NO_AUTH", "1")
    async with _make_client() as c:
        resp = await c.get("/cabinet/", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    assert "Cabinet" in resp.text or "cabinet" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "", "no"])
async def test_cabinet_bypass_disabled_when_env_falsy(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsy / unset FAKE_TORGI_NO_AUTH keeps the auth requirement."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    monkeypatch.setenv("FAKE_TORGI_NO_AUTH", value)
    async with _make_client() as c:
        resp = await c.get("/cabinet/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/fake-esia/authorize" in resp.headers["location"]


@pytest.mark.asyncio
async def test_list_pagination_page_2_returns_empty_when_lots_fit_on_page_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """page=2 must produce an empty tbody when all lots fit on page 1.

    This is the stop signal PaginatedListFetcher.iterate() looks for —
    without it, backfill walks pages indefinitely against fake_torgi (the
    bug behind gektar_monitor-ygp8).
    """
    from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

    lots_path = tmp_path / "lots.json"
    lots_path.write_text(
        json.dumps(
            [
                {
                    "id": i,
                    "cadastral_no": f"01:01:0000000:{i:03d}",
                    "area_sqm": 1000 + i,
                    "region": "Республика Адыгея",
                    "municipality": "тест",
                    "land_category": "Земли сельскохозяйственного назначения",
                    "permitted_use": "тест",
                    "ogv": "тест",
                    "date_create": "01.01.2026",
                    "date_update": "",
                    "status": "Свободен",
                }
                for i in range(1, 9)
            ]
        )
    )
    _patch_lots(monkeypatch, lots_path)
    async with _make_authed_client() as c:
        resp = await c.get(
            "/cabinet/free-lot", params={"region": 1, "page": 2, "per-page": 20}
        )
    assert resp.status_code == 200
    parsed = SelectolaxListParser().parse(resp.text)
    assert parsed.rows == []
    # total_count is the catalogue size — same on every page (real-site behaviour).
    assert parsed.total_count == 8


@pytest.mark.asyncio
async def test_list_pagination_slices_lots_per_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """per-page=3 splits 8 lots across 3 pages (3 + 3 + 2)."""
    from fis_monitor.infra.parsers.list_parser import SelectolaxListParser

    lots_path = tmp_path / "lots.json"
    lots_path.write_text(
        json.dumps(
            [
                {
                    "id": i,
                    "cadastral_no": f"01:01:0000000:{i:03d}",
                    "area_sqm": 1000 + i,
                    "region": "Республика Адыгея",
                    "municipality": "тест",
                    "land_category": "Земли сельскохозяйственного назначения",
                    "permitted_use": "тест",
                    "ogv": "тест",
                    "date_create": "01.01.2026",
                    "date_update": "",
                    "status": "Свободен",
                }
                for i in range(1, 9)
            ]
        )
    )
    _patch_lots(monkeypatch, lots_path)
    parser = SelectolaxListParser()
    async with _make_authed_client() as c:
        page1 = parser.parse(
            (
                await c.get(
                    "/cabinet/free-lot",
                    params={"region": 1, "page": 1, "per-page": 3},
                )
            ).text
        )
        page3 = parser.parse(
            (
                await c.get(
                    "/cabinet/free-lot",
                    params={"region": 1, "page": 3, "per-page": 3},
                )
            ).text
        )
        page4 = parser.parse(
            (
                await c.get(
                    "/cabinet/free-lot",
                    params={"region": 1, "page": 4, "per-page": 3},
                )
            ).text
        )
    assert len(page1.rows) == 3
    assert len(page3.rows) == 2  # 7th and 8th lots
    assert page4.rows == []  # past the end → stop signal
    assert page1.total_count == 8
    assert page3.total_count == 8
    assert page4.total_count == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_uri",
    [
        "/cabinet/../../../etc/passwd",  # path traversal
        "/\\evil.com",  # backslash trick (legacy browser quirk)
        "/foo/../bar",  # traversal even if path is harmless
    ],
)
async def test_redirect_uri_rejects_traversal_and_backslash(
    redirect_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_safe_redirect_uri must reject path traversal and backslash sequences."""
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
        resp = await c.post(
            "/fake-esia/login",
            data={"redirect_uri": redirect_uri},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/cabinet/"


# ---------------------------------------------------------------------------
# Isolation contract
# ---------------------------------------------------------------------------


def test_server_does_not_import_fis_monitor() -> None:
    """tools/fake_torgi/server.py may only import fis_monitor.domain.regions.

    ADR-054 explicitly allows this one import (read-only canonical region catalog
    SUBJECT_TITLE_BY_ID) to keep seed-data validation in sync with the parser.
    Any other fis_monitor import remains forbidden: services, infra, web must not
    be reachable from this staging tool.
    """
    import ast

    # The one permitted fis_monitor import (ADR-054).
    _ALLOWED_MODULES = {"fis_monitor.domain.regions"}

    source = (_REPO_ROOT / "tools" / "fake_torgi" / "server.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("fis_monitor"), (
                    f"tools/fake_torgi/server.py must not import fis_monitor, "
                    f"found: import {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            "fis_monitor"
        ):
            assert node.module in _ALLOWED_MODULES, (
                f"tools/fake_torgi/server.py may only import fis_monitor.domain.regions "
                f"(ADR-054), found: from {node.module} import ..."
            )
