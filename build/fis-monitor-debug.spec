# -*- mode: python ; coding: utf-8 -*-
"""Debug variant spec — produces fis-monitor-debug binary with DEBUG log level by default.

Identical to fis-monitor.spec except:
- runtime_hooks: injects FIS_LOG_LEVEL_DEFAULT=DEBUG before app code runs.
- name: "fis-monitor-debug" in EXE and COLLECT.

See: docs/decisions/ADR-026-distribution-packaging-pyinstaller.md
"""

import sys
from pathlib import Path

# Project root = parent of build/ directory that holds this spec.
_SPEC_DIR = Path(SPECPATH)  # noqa: F821  (PyInstaller injects SPECPATH)
_PROJECT_ROOT = _SPEC_DIR.parent
_SRC = _PROJECT_ROOT / "src" / "fis_monitor"

block_cipher = None

# ---------------------------------------------------------------------------
# Entry-point script (generated at build time from the console_scripts shim)
# ---------------------------------------------------------------------------
a = Analysis(
    [str(_SRC / "app.py")],
    pathex=[str(_PROJECT_ROOT / "src")],
    binaries=[],
    datas=[
        # Templates and static assets must travel with the binary.
        # Destination paths mirror the package layout so Path(__file__).parent
        # resolution in templates.py continues to work inside _internal/.
        (str(_SRC / "web" / "templates"), "fis_monitor/web/templates"),
        (str(_SRC / "web" / "static"), "fis_monitor/web/static"),
        # SQL schema used by the DB layer.
        # composition.py resolves:  Path(__file__).resolve().parent.parent.parent / "docs/db/schema.sql"
        # In --onedir: __file__ = _internal/fis_monitor/composition.py → .parent×3 = _internal/
        # So the schema must land at _internal/docs/db/schema.sql.
        (str(_PROJECT_ROOT / "docs" / "db" / "schema.sql"), "docs/db"),
    ],
    hiddenimports=[
        # fis_monitor.composition is loaded via importlib.import_module() in
        # app.main() — static analysis cannot see it.
        "fis_monitor.composition",
        # python-multipart — starlette imports it lazily via try/except when
        # a request body is form-data.  Without explicit pin form-parsing
        # raises AssertionError "The `python-multipart` library must be installed".
        "multipart",
        # uvicorn optional sub-modules discovered at runtime.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # watchdog platform backends — picked at runtime by importlib.
        "watchdog.observers",
        "watchdog.observers.inotify",
        "watchdog.observers.fsevents",
        "watchdog.observers.winapi",
        # platformdirs — may use lazy imports internally.
        "platformdirs",
        # psutil C extensions are sometimes missed on Linux.
        "psutil._pslinux",
        "psutil._psposix",
        "psutil._pswindows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(_SPEC_DIR / "rthook_debug.py")],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # --onedir: keep binaries separate in _internal/
    name="fis-monitor-debug",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can corrupt Playwright's bundled Chromium helpers
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="fis-monitor-debug",
)
