# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for fis-monitor distribution archives.

Targets: Linux x86_64, Windows x86_64 (build-on-target strategy, ADR-022-dist).
Mode: --onedir (NOT --onefile) — avoids slow tmpdir extraction on every launch.

Resource resolution: src/fis_monitor/web/templates.py uses Path(__file__).parent /
"templates" and Path(__file__).parent / "static".  PyInstaller --onedir preserves
this layout inside _internal/, so no sys._MEIPASS patching is needed.

To add a new platform: create a separate build script (scripts/build_release_<os>.sh)
and run this spec there.  Do NOT modify this spec for per-platform differences.
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
    runtime_hooks=[],
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
    name="fis-monitor",
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
    name="fis-monitor",
)
