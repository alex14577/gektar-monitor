"""Playwright runtime pre-flight check.

Filesystem-only — does NOT import or invoke playwright (which would crash
inside the asyncio lifespan, since sync_playwright cannot run on an event
loop). Checks the standard browser cache locations for a chromium binary.

Verifies that the bundled Chromium binary is installed and executable.
Called from lifespan; on failure, logs ERROR and returns False so the
container can mark the login service as unavailable without crashing
the whole app (other features — feed, settings, onboarding — still work).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_log = logging.getLogger(__name__)


def _default_browsers_path() -> Path:
    """Return the platform-specific default location for Playwright browsers."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    # Linux / WSL
    return Path.home() / ".cache" / "ms-playwright"


def _binary_candidates(chromium_dir: Path) -> list[Path]:
    """Return platform-specific chromium binary candidate paths inside <chromium_dir>.

    Supports both the legacy layout (chrome-linux/chrome, used by Playwright ≤1.40)
    and the modern layout (chrome-linux64/chrome, chrome-headless-shell-linux64/
    chrome-headless-shell, used by Playwright ≥1.41+).
    """
    if sys.platform == "darwin":
        return [
            chromium_dir / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            # Modern layout (Playwright ≥1.41)
            chromium_dir / "chrome-mac-x64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            chromium_dir / "chrome-mac-arm64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
        ]
    if sys.platform == "win32":
        return [
            chromium_dir / "chrome-win" / "chrome.exe",
            chromium_dir / "chrome-win64" / "chrome.exe",
        ]
    # Linux / WSL — legacy and modern layouts
    return [
        # Modern layout (Playwright ≥1.41)
        chromium_dir / "chrome-linux64" / "chrome",
        chromium_dir / "chrome-headless-shell-linux64" / "chrome-headless-shell",
        # Legacy layout (Playwright ≤1.40)
        chromium_dir / "chrome-linux" / "chrome",
        chromium_dir / "chrome-linux" / "headless_shell",
    ]


def chromium_executable_exists() -> bool:
    """Return True iff a Playwright chromium binary is present and executable.

    Uses pure filesystem checks — no playwright runtime imports — so it is
    safe to call from inside the asyncio event loop (e.g. lifespan startup).
    Accepts either full chromium or headless-shell installs.
    """
    base = _default_browsers_path()
    if not base.is_dir():
        _log.debug("preflight: browsers dir not found at %s", base)
        return False
    # Match both `chromium-<rev>` (full) and `chromium_headless_shell-<rev>`.
    candidates = sorted(
        list(base.glob("chromium-*")) + list(base.glob("chromium_headless_shell-*")),
        reverse=True,
    )
    for chromium_dir in candidates:
        for binary in _binary_candidates(chromium_dir):
            try:
                if binary.is_file() and os.access(binary, os.X_OK):
                    _log.debug("preflight: chromium binary found at %s", binary)
                    return True
            except OSError:  # pragma: no cover — defensive
                continue
    _log.debug("preflight: no chromium binary found under %s", base)
    return False
