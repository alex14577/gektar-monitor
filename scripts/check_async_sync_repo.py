"""CI guard: detect async route handlers that call sync SQLite repo methods directly.

Problem: an ``async def`` FastAPI handler that calls a synchronous SQLite-backed
repository method (e.g. ``cycles_repo.get(...)``) without wrapping in
``asyncio.to_thread`` holds the SQLite writer lock on the event-loop thread,
blocking all other coroutines (incident: bd 45el).

Usage::

    python scripts/check_async_sync_repo.py src/fis_monitor/web/routes/
    python scripts/check_async_sync_repo.py src/fis_monitor/web/routes/main.py

Exit 0  — no violations found.
Exit 1  — at least one violation; details written to stderr in
          ``path:line: message`` format (editor problem-matcher compatible).

Suppression: add ``# noqa: async-sync-repo`` at the end of the offending line.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Heuristic: repo variable-name suffixes that indicate SQLite repositories.
# A name is considered a "repo" if it equals one of these or ends with
# one of these as a word boundary (e.g. ``lot_repo``, ``cycles_repo``).
# ---------------------------------------------------------------------------
_REPO_SUFFIXES = (
    "notifications_repo",
    "lots_repo",
    "lot_repo",
    "cycles_repo",
    "cycle_repo",
    "user_state_repo",
    "region_subscriptions_repo",
    "region_subscription_repo",
    "settings_repo",
    "smtp_credentials_repo",
    "smtp_credential_repo",
    "state_repo",
)


def _is_repo_name(name: str) -> bool:
    """Return True if *name* looks like a SQLite repo variable."""
    return any(name == s or name.endswith("_" + s.split("_repo")[0] + "_repo")
               for s in _REPO_SUFFIXES)


def _extract_object_name(node: ast.expr) -> str | None:
    """Return the attribute root name for ``obj.method(...)`` call targets.

    Examples::

        cycles_repo.get(...)   → "cycles_repo"
        self.cycles_repo.get() → "cycles_repo"   (last Name before method)
    """
    if isinstance(node, ast.Attribute):
        return _extract_object_name(node.value)
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_to_thread_call(node: ast.expr) -> bool:
    """Return True if *node* is ``asyncio.to_thread(...)`` or
    ``anyio.to_thread.run_sync(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # asyncio.to_thread
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "to_thread"
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
    ):
        return True
    # anyio.to_thread.run_sync
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run_sync"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "to_thread"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "anyio"
    )


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _source_line(source_lines: list[str], lineno: int) -> str:
    """Return the source line (1-based) or empty string."""
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1]
    return ""


def _has_noqa(source_lines: list[str], lineno: int) -> bool:
    """Return True if the source line has ``# noqa: async-sync-repo``."""
    line = _source_line(source_lines, lineno)
    return "# noqa: async-sync-repo" in line


class _RepoCallVisitor(ast.NodeVisitor):
    """Walk the body of an async function looking for bare repo calls.

    A "bare" repo call is a ``Call`` node where:
    - the callee's object name looks like a repo (``_is_repo_name``), AND
    - the call is NOT the first argument of ``asyncio.to_thread`` /
      ``anyio.to_thread.run_sync``.
    """

    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.source_lines = source_lines
        self.violations: list[Violation] = []
        # Stack tracking whether we are inside a to_thread call's args.
        self._inside_to_thread = False

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Do not descend into nested async functions — the outer ast.walk in
        # check_file() reaches them independently, so descending here would
        # double-report any violations inside the inner function.
        return

    def visit_Call(self, node: ast.Call) -> None:
        if _is_to_thread_call(node):
            # Arguments of to_thread are safe — don't descend into them
            # looking for violations.  But we still need to visit keyword
            # arguments that are not the sync callable (none expected, but
            # be safe by visiting them without the to_thread flag).
            old = self._inside_to_thread
            self._inside_to_thread = True
            self.generic_visit(node)
            self._inside_to_thread = old
            return

        if (
            not self._inside_to_thread
            and isinstance(node.func, ast.Attribute)
            and (obj_name := _extract_object_name(node.func.value))
            and _is_repo_name(obj_name)
            and not _has_noqa(self.source_lines, node.lineno)
        ):
            self.violations.append(
                Violation(
                    path=self.path,
                    line=node.lineno,
                    message=(
                        f"async handler calls sync repo "
                        f"`{obj_name}.{node.func.attr}()` "
                        f"without asyncio.to_thread — "
                        f"blocks event loop (bd 45el). "
                        f"Suppress with: # noqa: async-sync-repo"
                    ),
                )
            )

        self.generic_visit(node)


def check_file(path: Path) -> list[Violation]:
    """Parse *path* and return all async-sync-repo violations.

    This is the primary entry point used by tests — it does NOT perform any
    file-system glob walking.  Walking is handled by ``check_paths`` (CLI).
    """
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            visitor = _RepoCallVisitor(path, source_lines)
            # Use visitor.visit(node) which triggers visit_AsyncFunctionDef
            # via generic_visit — but we only want the body of *this* node,
            # not nested AsyncFunctionDef bodies (the outer ast.walk will
            # reach those independently).  So we walk node.body directly.
            for stmt in node.body:
                visitor.visit(stmt)
            violations.extend(visitor.violations)

    return violations


def check_paths(paths: list[Path]) -> list[Violation]:
    """Walk *paths*, expanding directories to ``*.py`` globs recursively."""
    all_violations: list[Violation] = []
    for p in paths:
        if p.is_dir():
            for py_file in sorted(p.rglob("*.py")):
                all_violations.extend(check_file(py_file))
        elif p.is_file():
            all_violations.extend(check_file(p))
    return all_violations


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "Usage: check_async_sync_repo.py <path> [<path> ...]",
            file=sys.stderr,
        )
        return 2

    violations = check_paths([Path(a) for a in args])
    for v in violations:
        print(str(v), file=sys.stderr)

    if violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
