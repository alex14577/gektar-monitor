# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for gektar-gen-license standalone CLI.

Target: Linux x86_64, Windows x86_64 (build-on-target strategy).
Mode: --onefile — wraps everything in a single executable.

Rationale for --onefile (unlike fis-monitor which uses --onedir):
  ADR-026 rejected --onefile for fis-monitor due to 5-15 second cold-start
  from extracting Playwright Chromium (~280 MB) to $TMPDIR on every launch.
  gen-license has NO runtime resources (no templates, no Chromium, no data
  files) — the frozen archive is ~15 MB. Cold-start latency is <200 ms,
  which is acceptable for an interactive CLI tool used by authorized resellers.
  See ADR-059.

Excluded packages: the app-layer (fastapi, uvicorn, starlette, playwright,
  requests, selectolax, psutil, watchdog, jinja2, sse_starlette, multipart,
  urllib3) and fis_monitor subpackages that belong to the runtime distribution
  (app, composition, web, services, infra, domain) must NOT appear in this
  binary. The import-linter contract gen-license-cli-no-app-graph enforces
  this at CI time; the excludes list here is a defence-in-depth safeguard.
"""

from pathlib import Path

# Project root = parent of build/ directory that holds this spec.
_SPEC_DIR = Path(SPECPATH)  # noqa: F821  (PyInstaller injects SPECPATH)
_PROJECT_ROOT = _SPEC_DIR.parent

block_cipher = None

a = Analysis(
    [str(_PROJECT_ROOT / "src" / "fis_monitor" / "licensing" / "cli.py")],
    pathex=[str(_PROJECT_ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # App-layer: must never enter this binary (ADR-059 §size gate).
        "fis_monitor.app",
        "fis_monitor.composition",
        "fis_monitor.web",
        "fis_monitor.services",
        "fis_monitor.infra",
        "fis_monitor.domain",
        # Heavy 3rd-party deps from the runtime distribution (fis-monitor).
        # These are excluded to keep the binary small and to make bloat visible
        # early — if a future import accidentally pulls one of these in, the
        # size gate in build_gen_license.sh will fail before the binary ships.
        "fastapi",
        "starlette",
        "uvicorn",
        "sse_starlette",
        "jinja2",
        "markupsafe",
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
        "selectolax",
        "playwright",
        "psutil",
        "watchdog",
        "multipart",
        "tzdata",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    exclude_binaries=False,  # --onefile: bundle everything into the executable
    name="gektar-gen-license",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX disabled: no measurable benefit at ~15 MB; avoids AV false-positives
    console=True,  # console=True required for interactive stdio prompts
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# No COLLECT block — --onefile mode (exclude_binaries=False) produces a single
# self-contained executable. COLLECT is only needed for --onedir.
