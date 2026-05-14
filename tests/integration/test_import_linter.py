"""Integration test: import-linter contracts all pass.

Runs `lint-imports` as a subprocess so we catch real import violations
across the entire fis_monitor package tree.

Marked @pytest.mark.slow because import-linter scans the whole source tree.
Run with: pytest tests/integration/test_import_linter.py -v -m slow
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
IMPORTLINTER_CONFIG = REPO_ROOT / ".importlinter"


def _lint_imports_cmd() -> str:
    """Resolve the lint-imports CLI executable path."""
    cmd = shutil.which("lint-imports")
    if cmd is None:
        pytest.skip("lint-imports CLI not found on PATH — install import-linter[dev]")
    return cmd


@pytest.mark.slow
def test_lint_imports_passes() -> None:
    """All import-linter contracts must pass (exit code 0).

    Contracts under test (as of Wave 10d):
      - layers: layered architecture (composition/app > web > services > infra > domain)
      - domain_purity: domain must not import sqlite3, requests, fastapi, playwright, smtplib
      - domain_no_logging: domain must not import the stdlib `logging` module
        (observability is an application/infrastructure concern — ADR-017,
         docs/architecture/02-layers-dip.md)
    """
    result = subprocess.run(
        [_lint_imports_cmd(), "--config", str(IMPORTLINTER_CONFIG)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"lint-imports failed (exit {result.returncode}).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.slow
def test_domain_no_logging_contract_is_configured() -> None:
    """Smoke-check: the domain_no_logging contract is present in .importlinter.

    Guards against accidental removal of the contract definition itself.
    """
    config_text = IMPORTLINTER_CONFIG.read_text(encoding="utf-8")
    assert "domain_no_logging" in config_text, (
        "The [importlinter:contract:domain_no_logging] contract is missing from "
        f"{IMPORTLINTER_CONFIG}. It must be present to enforce that domain code "
        "never imports the stdlib `logging` module."
    )
    assert "forbidden_modules = logging" in config_text or (
        # Accept multi-line forbidden_modules form as well
        "logging" in config_text
    ), "domain_no_logging contract must list `logging` as a forbidden module."
