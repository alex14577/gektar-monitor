"""Tests for scripts/check_async_sync_repo.py.

Covers the five invariants specified in bd vvql:
  1. Async handler with direct repo call       → VIOLATION reported (path:line).
  2. Async handler with asyncio.to_thread wrap → CLEAN.
  3. Sync def handler with direct repo call    → CLEAN.
  4. Async handler with # noqa: async-sync-repo on offending call → CLEAN.
  5. Async handler calling a non-repo callable → CLEAN.

Tests use tmp_path fixtures with synthetic route file content.
File-system glob walking (check_paths) is NOT tested here — that is plumbing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make scripts/ importable without installing the package.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_async_sync_repo import Violation, check_file  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "routes.py"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Invariant 1: async handler + direct repo call → VIOLATION
# ---------------------------------------------------------------------------


def test_async_handler_direct_repo_call_is_violation(tmp_path: Path) -> None:
    src = """\
async def get_cycles(cycles_repo):
    result = cycles_repo.get(123)
    return result
"""
    violations = check_file(_write(tmp_path, src))
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, Violation)
    assert v.line == 2
    assert "cycles_repo.get()" in v.message


def test_violation_message_contains_path_and_line(tmp_path: Path) -> None:
    src = """\
async def handler(lot_repo):
    return lot_repo.count_active()
"""
    p = _write(tmp_path, src)
    violations = check_file(p)
    assert violations
    formatted = str(violations[0])
    assert str(p) in formatted
    assert ":2:" in formatted


# ---------------------------------------------------------------------------
# Invariant 2: async handler + asyncio.to_thread wrap → CLEAN
# ---------------------------------------------------------------------------


def test_asyncio_to_thread_wrap_is_clean(tmp_path: Path) -> None:
    src = """\
import asyncio

async def get_cycles(cycles_repo):
    result = await asyncio.to_thread(cycles_repo.get, 123)
    return result
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


def test_anyio_to_thread_run_sync_is_clean(tmp_path: Path) -> None:
    src = """\
import anyio

async def get_cycles(cycles_repo):
    result = await anyio.to_thread.run_sync(cycles_repo.get, 123)
    return result
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


# ---------------------------------------------------------------------------
# Invariant 3: sync def handler + direct repo call → CLEAN
# ---------------------------------------------------------------------------


def test_sync_handler_direct_repo_call_is_clean(tmp_path: Path) -> None:
    src = """\
def get_cycles(cycles_repo):
    result = cycles_repo.get(123)
    return result
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


# ---------------------------------------------------------------------------
# Invariant 4: # noqa: async-sync-repo suppresses violation
# ---------------------------------------------------------------------------


def test_noqa_comment_suppresses_violation(tmp_path: Path) -> None:
    src = """\
async def handler(cycles_repo):
    return cycles_repo.get(1)  # noqa: async-sync-repo
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


def test_noqa_on_different_line_does_not_suppress(tmp_path: Path) -> None:
    src = """\
async def handler(cycles_repo):
    # noqa: async-sync-repo
    return cycles_repo.get(1)
"""
    violations = check_file(_write(tmp_path, src))
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# Invariant 5: async handler calling a non-repo callable → CLEAN
# ---------------------------------------------------------------------------


def test_non_repo_callable_is_clean(tmp_path: Path) -> None:
    src = """\
async def handler(helpers):
    return helpers.foo()
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


def test_non_repo_method_on_repo_like_object_name_not_matched(tmp_path: Path) -> None:
    """A variable named 'helper' (not ending in _repo) calling a method → CLEAN."""
    src = """\
async def handler(service):
    service.do_something()
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_multiple_violations_reported(tmp_path: Path) -> None:
    src = """\
async def handler(lot_repo, user_state_repo):
    a = lot_repo.count_active()
    b = user_state_repo.last_visit()
    return a, b
"""
    violations = check_file(_write(tmp_path, src))
    assert len(violations) == 2
    lines = {v.line for v in violations}
    assert lines == {2, 3}


def test_clean_file_returns_empty_list(tmp_path: Path) -> None:
    src = """\
def helper():
    pass

async def handler(request):
    return {"ok": True}
"""
    violations = check_file(_write(tmp_path, src))
    assert violations == []
