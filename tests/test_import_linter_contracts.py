"""ADR-006: import-linter contracts — layered architecture + domain purity.

Two tests:
1. Happy path — real .importlinter config must pass (exit 0).
2. Negative — a deliberately broken contract must be detected (exit != 0).
"""

import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _resolve_lint_imports() -> str:
    """Find lint-imports binary co-located with the active Python interpreter.

    Returns the absolute path or skips the test if not installed.
    """
    venv_bin = pathlib.Path(sys.executable).parent
    candidate = venv_bin / "lint-imports"
    if candidate.exists():
        return str(candidate)
    # Fallback to PATH search
    found = shutil.which("lint-imports")
    if found is None:
        pytest.skip("import-linter not installed in active environment")
    return found


def test_import_linter_contracts_pass() -> None:
    """ADR-006: layered architecture + domain purity contracts must hold."""
    linter = _resolve_lint_imports()
    result = subprocess.run(
        [linter, "--config", ".importlinter"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"import-linter failed:\n{result.stdout}\n{result.stderr}"
    )


def test_import_linter_detects_broken_contract(tmp_path: pathlib.Path) -> None:
    """import-linter must fail when a contract is violated.

    Strategy: write a config with a forbidden-contract that is *guaranteed*
    to be broken — declare that fis_monitor.infra must not import
    fis_monitor.domain.  infra legitimately imports domain (onion pattern),
    so this contract is always violated on the current codebase.
    """
    linter = _resolve_lint_imports()
    broken_config = tmp_path / ".importlinter"
    broken_config.write_text(
        "[importlinter]\n"
        "root_package = fis_monitor\n"
        "\n"
        "[importlinter:contract:infra_must_not_import_domain]\n"
        "name = DELIBERATELY BROKEN — infra must not import domain\n"
        "type = forbidden\n"
        "source_modules = fis_monitor.infra\n"
        "forbidden_modules = fis_monitor.domain\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [linter, "--config", str(broken_config)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0, (
        "Expected import-linter to report a violation, but it exited 0.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
