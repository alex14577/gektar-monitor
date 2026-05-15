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
    async with _make_client() as c:
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
    async with _make_client() as c:
        resp = await c.get("/cabinet/free-lot", params={"region": 1})
    rows = SelectolaxListParser().parse(resp.text).rows
    assert rows == []


@pytest.mark.asyncio
async def test_detail_endpoint_returns_200_with_main_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_lots(monkeypatch, tmp_path / "lots.json")
    async with _make_client() as c:
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
    async with _make_client() as c:
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
    async with _make_client() as c:
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
    async with _make_client() as c:
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
    async with _make_client() as c:
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
# Isolation contract
# ---------------------------------------------------------------------------


def test_server_does_not_import_fis_monitor() -> None:
    """tools/fake_torgi/server.py must NOT import from fis_monitor.

    Enforces the isolation contract (ADR-006 spirit): tools/ is a dev utility,
    not part of the application package.  A violation would mean the staging
    server silently depends on internal implementation details.
    """
    import ast

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
            raise AssertionError(
                f"tools/fake_torgi/server.py must not import fis_monitor, "
                f"found: from {node.module} import ..."
            )
