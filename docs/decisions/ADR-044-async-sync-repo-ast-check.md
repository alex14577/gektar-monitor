# ADR-044: AST-based CI guard for async handlers calling sync SQLite repos

**Status:** Accepted — 2026-05-17 (bd `gektar_monitor-vvql`)

## Context

Incident bd 45el: an `async def` FastAPI route handler called a synchronous SQLite-backed repository method directly (without `asyncio.to_thread`). While the SQLite writer lock was held, the event loop was blocked — all other coroutines stalled. The fix was to wrap the call in `await asyncio.to_thread(...)`. Without a CI guard, this pattern can be silently reintroduced.

The pattern is: any `async def` function in `src/fis_monitor/web/routes/*.py` whose body contains a `*_repo.<method>(...)` call NOT wrapped inside `asyncio.to_thread(...)` or `anyio.to_thread.run_sync(...)`.

## Decision

Implement a **self-contained AST analyzer** (`scripts/check_async_sync_repo.py`, stdlib only):

- Walks `src/fis_monitor/web/routes/` (`*.py`).
- For each `AsyncFunctionDef` node: visits child `Call` nodes.
- A call is a violation when: the callee's receiver name ends in a known `_repo` suffix AND the call is not inside an `asyncio.to_thread` / `anyio.to_thread.run_sync` argument position.
- Allow-list: `# noqa: async-sync-repo` on the offending line suppresses the check (escape hatch for documented exceptions).
- Output: `path:line: <message>` to stderr (editor problem-matcher compatible). Exit 1 on any violation.

Entry point: `python scripts/check_async_sync_repo.py src/fis_monitor/web/routes/`.

Wiring: invoke directly via `python scripts/...` (no Makefile in project). CI workflow (`.github/workflows/ci.yml`) should add a step — that is bd vgm.4's scope.

## Alternatives considered

### Custom Ruff rule (plugin)
Ruff supports custom AST plugins via `ruff-plugin-*` crates. Rejected: requires Rust toolchain + separate compile step, significantly more complex for a project-specific heuristic that changes rarely.

### import-linter contract
`import-linter` works at module-import graph level — it can forbid `routes → infra.sqlite`, but cannot detect **which functions** do the calling or whether those functions are `async`. Would produce false positives (sync routes legitimately call repos in sync functions) and false negatives (indirect calls). Rejected.

### Mypy plugin / Protocol check
Would require adding `Async` variants of every repo Protocol and enforcing return types. High complexity, invasive to existing architecture. Rejected.

## Consequences

**Positive:**
- Prevents regression of bd 45el class of bugs at CI time.
- Zero runtime overhead; zero new dependencies.
- Escape hatch (`# noqa: async-sync-repo`) preserves flexibility for documented edge cases.

**Negative:**
- Heuristic is name-based (`_repo` suffix); if a repo is named unconventionally it won't be caught. Acceptable: project convention enforces `*_repo` naming (ADR-016).
- Must update `_REPO_SUFFIXES` list in the script when new repo types are introduced.

## References

- bd incident: `gektar_monitor-45el`
- [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]] — repository invariants
- [[decisions/ADR-005-concurrency-soft-yield-retry-busy|ADR-005]] — concurrency model
