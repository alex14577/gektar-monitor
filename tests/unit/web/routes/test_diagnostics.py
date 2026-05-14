"""Unit tests for POST /diagnostics/build route.

Uses TestClient + app.dependency_overrides with a FakeDiagnosticsService.

Key scenario:
  #21 — schema drift → 503 with generic ui_message (no internal details
        leaked, R3-M5 / R4-M10).
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.services.diagnostics import BuildZipResult
from fis_monitor.web.deps import get_diagnostics
from fis_monitor.web.routes.diagnostics import router

# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------

_GENERIC_UI_MESSAGE = "Diagnostics bundle unavailable. Please contact support."


class FakeDiagnosticsService:
    """Fake for DiagnosticsService — implements ALL public methods.

    Configurable to simulate:
      - success (ok=True, schema_ok=True)
      - schema drift fail-closed (ok=False, schema_ok=False)
      - generic build failure (ok=False, schema_ok=True)
      - unexpected exception (raise_exc=True)
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        schema_ok: bool = True,
        ui_message: str = "",
        write_zip: bool = False,
        raise_exc: bool = False,
    ) -> None:
        self._ok = ok
        self._schema_ok = schema_ok
        self._ui_message = ui_message
        self._write_zip = write_zip
        self._raise_exc = raise_exc
        # Call tracking
        self.build_zip_calls: list[Path] = []

    def build_zip(self, output_path: Path) -> BuildZipResult:
        self.build_zip_calls.append(output_path)
        if self._raise_exc:
            raise RuntimeError("unexpected internal error")
        if self._write_zip and self._ok:
            # Write a minimal valid zip so FileResponse can serve it.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as zf:
                zf.writestr("info.txt", "diagnostic data")
        return BuildZipResult(
            ok=self._ok,
            output_path=output_path,
            files_included=("info.txt",) if self._ok else (),
            schema_ok=self._schema_ok,
            audit_included=True,
            ui_message=self._ui_message,
        )


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(
    fake: FakeDiagnosticsService | None = None,
) -> tuple[FastAPI, FakeDiagnosticsService]:
    if fake is None:
        fake = FakeDiagnosticsService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_diagnostics] = lambda: fake
    return app, fake


# ---------------------------------------------------------------------------
# Tests — success path
# ---------------------------------------------------------------------------


def test_build_diagnostics_success_returns_zip() -> None:
    """ok=True → 200 with application/zip content-type."""
    fake = FakeDiagnosticsService(ok=True, schema_ok=True, write_zip=True)
    app, fake = _make_app(fake)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/diagnostics/build")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert len(fake.build_zip_calls) == 1


# ---------------------------------------------------------------------------
# Test #21 — schema drift → 503, generic message, no PII / detail leak
# ---------------------------------------------------------------------------


def test_build_diagnostics_schema_drift_returns_503() -> None:
    """Schema drift (schema_ok=False) → 503.

    Acceptance criterion #21: fail-closed guard returns generic ui_message
    with no internal details (column names, paths, schema info).
    """
    fake = FakeDiagnosticsService(
        ok=False,
        schema_ok=False,
        ui_message=_GENERIC_UI_MESSAGE,
    )
    app, fake = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/diagnostics/build")

    assert resp.status_code == 503
    body = resp.json()
    assert "error" in body
    assert "ui_message" in body
    assert body["ui_message"] == _GENERIC_UI_MESSAGE
    # Anti-leak: no column names, paths, or schema details in response
    assert "schema" not in body["ui_message"].lower() or "unavailable" in body["ui_message"].lower()
    assert len(fake.build_zip_calls) == 1


def test_build_diagnostics_schema_drift_no_detail_leak() -> None:
    """Response body must NOT contain internal details (PII/schema info leak guard)."""
    fake = FakeDiagnosticsService(
        ok=False,
        schema_ok=False,
        ui_message=_GENERIC_UI_MESSAGE,
    )
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/diagnostics/build")

    text = resp.text
    # Must not leak column names, table names, or file paths
    assert "column" not in text
    assert "table=" not in text
    assert "/home" not in text
    assert "SchemaDrift" not in text
    # Must not leak temp file paths or internal module names (m3)
    assert "/tmp" not in text
    assert "diag" not in text.lower().replace("diagnostics", "").replace("diagnostic.zip", "")


# ---------------------------------------------------------------------------
# Tests — generic build failure
# ---------------------------------------------------------------------------


def test_build_diagnostics_build_failure_returns_500() -> None:
    """ok=False, schema_ok=True → 500."""
    fake = FakeDiagnosticsService(
        ok=False,
        schema_ok=True,
        ui_message=_GENERIC_UI_MESSAGE,
    )
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/diagnostics/build")
    assert resp.status_code == 500
    body = resp.json()
    assert "error" in body
    assert "ui_message" in body


# ---------------------------------------------------------------------------
# Tests — exception during build_zip cleans up temp file (M1)
# ---------------------------------------------------------------------------


def test_build_diagnostics_exception_returns_500_and_cleans_up() -> None:
    """build_zip raises RuntimeError → 500 and temp file is deleted (M1 leak guard)."""
    fake = FakeDiagnosticsService(raise_exc=True)
    app, _ = _make_app(fake)
    captured_path: list[Path] = []

    original_build_zip = fake.build_zip

    def capturing_build_zip(output_path: Path) -> BuildZipResult:
        captured_path.append(output_path)
        return original_build_zip(output_path)

    fake.build_zip = capturing_build_zip  # type: ignore[method-assign]

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/diagnostics/build")

    assert resp.status_code == 500
    # Temp file must be cleaned up after the exception
    if captured_path:
        assert not captured_path[0].exists(), "temp file must be deleted on exception"


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods in one test
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Verify every method of FakeDiagnosticsService is callable (anti-mock §6)."""
    fake = FakeDiagnosticsService(ok=True, schema_ok=True, write_zip=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "diag.zip"
        result = fake.build_zip(path)

    assert result.ok is True
    assert result.schema_ok is True
    assert len(fake.build_zip_calls) == 1
    assert fake.build_zip_calls[0] == path
