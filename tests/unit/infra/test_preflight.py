"""Unit tests for the Playwright pre-flight check.

Coverage (Layer 3 — infra):
  Filesystem-based checks via tmp_path + monkeypatch(PLAYWRIGHT_BROWSERS_PATH).
  No playwright runtime is imported or invoked.

  1. Returns False when browsers dir is absent.
  2. Returns False when browsers dir is empty.
  3. Returns True for a full chromium install (modern chrome-linux64 layout).
  4. Returns True for a headless-shell-only install (modern chrome-headless-shell-linux64 layout).
  5. Returns False when binary exists but is not executable.
  6. Returns True when multiple revisions exist.
  7. _default_browsers_path returns an absolute path containing 'ms-playwright'
     when PLAYWRIGHT_BROWSERS_PATH is not set.
  8. Returns True for legacy chrome-linux layout (Playwright ≤1.40 compat).
"""

from __future__ import annotations

import os
import sys

import pytest

from fis_monitor.infra.playwright.preflight import (
    _default_browsers_path,
    chromium_executable_exists,
)


@pytest.fixture
def fake_browsers_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    return tmp_path


def _make_chromium(
    root,
    *,
    revision: str,
    headless_only: bool = False,
    executable: bool = True,
    legacy: bool = False,
):
    """Create a fake chromium layout under root.

    By default uses the modern layout (Playwright ≥1.41):
      - Linux full:     chromium-<rev>/chrome-linux64/chrome
      - Linux headless: chromium_headless_shell-<rev>/
                        chrome-headless-shell-linux64/chrome-headless-shell
    With legacy=True uses the old layout (Playwright ≤1.40):
      - Linux full:     chromium-<rev>/chrome-linux/chrome
      - Linux headless: chromium_headless_shell-<rev>/chrome-linux/headless_shell
    """
    if sys.platform == "win32":
        binary = root / f"chromium-{revision}" / "chrome-win64" / "chrome.exe"
    elif sys.platform == "darwin":
        binary = (
            root
            / f"chromium-{revision}"
            / "chrome-mac"
            / "Chromium.app"
            / "Contents"
            / "MacOS"
            / "Chromium"
        )
    else:
        if legacy:
            name = "headless_shell" if headless_only else "chrome"
            dirname = (
                f"chromium_headless_shell-{revision}" if headless_only else f"chromium-{revision}"
            )
            binary = root / dirname / "chrome-linux" / name
        elif headless_only:
            binary = (
                root
                / f"chromium_headless_shell-{revision}"
                / "chrome-headless-shell-linux64"
                / "chrome-headless-shell"
            )
        else:
            binary = root / f"chromium-{revision}" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\necho fake")
    if executable:
        os.chmod(binary, 0o755)
    return binary


def test_returns_false_when_browsers_dir_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing"))
    assert chromium_executable_exists() is False


def test_returns_false_when_browsers_dir_empty(fake_browsers_root):
    assert chromium_executable_exists() is False


def test_returns_true_for_full_chromium(fake_browsers_root):
    _make_chromium(fake_browsers_root, revision="1208")
    assert chromium_executable_exists() is True


def test_returns_true_for_headless_only(fake_browsers_root):
    _make_chromium(fake_browsers_root, revision="1208", headless_only=True)
    assert chromium_executable_exists() is True


def test_returns_false_when_binary_not_executable(fake_browsers_root):
    if sys.platform == "win32":
        pytest.skip("windows has no executable bit")
    _make_chromium(fake_browsers_root, revision="1208", executable=False)
    assert chromium_executable_exists() is False


def test_picks_newest_revision_when_multiple(fake_browsers_root):
    _make_chromium(fake_browsers_root, revision="1100")
    _make_chromium(fake_browsers_root, revision="1208")
    assert chromium_executable_exists() is True


def test_default_browsers_path_platform_default(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    path = _default_browsers_path()
    assert path.is_absolute()
    assert "ms-playwright" in str(path)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only legacy layout test")
def test_returns_true_for_legacy_chrome_linux_layout(fake_browsers_root):
    """Legacy Playwright ≤1.40 layout (chrome-linux/chrome) is still detected."""
    _make_chromium(fake_browsers_root, revision="1000", legacy=True)
    assert chromium_executable_exists() is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only legacy headless layout test")
def test_returns_true_for_legacy_headless_shell_layout(fake_browsers_root):
    """Legacy headless_shell layout (chrome-linux/headless_shell) is still detected."""
    _make_chromium(fake_browsers_root, revision="1000", headless_only=True, legacy=True)
    assert chromium_executable_exists() is True
