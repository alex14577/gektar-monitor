# ADR-045 — GitHub Actions CI Pipeline

**Status:** Accepted  
**Date:** 2026-05-17  
**BD task:** `gektar_monitor-vgm.4`

---

## Context

The project had a Release workflow (`release.yml`) for PyInstaller packaging on tag push, but no
blocking quality gates on `master`/`main` branches or pull requests. This created a gap where
ruff errors, mypy regressions, import-linter contract violations, and test failures could land
on master undetected until the release build surfaced them.

Existing tooling already defined in `pyproject.toml [dev]`:
- `ruff>=0.6` — linting
- `mypy>=1.13` — type checking
- `import-linter>=2.0` — layer contracts (5 contracts, `.importlinter` at repo root)
- `pytest>=8.0` + `pytest-cov>=5.0` + `pytest-asyncio>=0.23` — unit/integration tests
- `scripts/check_async_sync_repo.py` — AST guard for async routes calling sync repos (ADR-044)

Coverage baseline measured at time of implementation: **91.93%** on `domain/` + `services/` with
`-m "not slow"`. Two under-covered files noted: `full_scan.py` (68%) and `lot_user_state.py` (40%).

---

## Decision

Create `.github/workflows/ci.yml` with **four independent jobs**:

| Job | Concern | Timeout |
|-----|---------|---------|
| `lint` | ruff + import-linter + async-sync-repo | 10 min |
| `typecheck` | mypy | 10 min |
| `test-unit` | pytest `-m "not slow"` + coverage gate ≥80% on domain+services | 15 min |
| `test-integration` | pytest `-m slow` | 30 min |

All four jobs are **blocking** (no `continue-on-error`). Failure in any job blocks merge.

**Coverage gate** is set at `--cov-fail-under=80` (acceptance requirement). Observed baseline of
92% provides headroom. The gate is scoped to `domain/` + `services/` only — not `web/` or
`infra/` — matching the acceptance criteria and the test-strategy layers where unit tests are
authoritative (ADR-041).

**Concurrency control:** `cancel-in-progress: true` on `ci-${{ github.ref }}` to avoid queuing
stale runs when new commits land on the same branch.

**Python version:** 3.12 (matches `release.yml`).

**Install strategy:** `pip install -e ".[dev]"` — the project defines
`[project.optional-dependencies] dev` containing all required tools, making this a single command
with no fragile explicit lists.

---

## Alternatives considered

### Single-job pipeline
All steps in one job. Simpler YAML, one log to read.  
**Rejected:** any failure masks the rest; lint failure hides whether tests pass. Split jobs give
precise failure localization and allow parallel execution by GitHub's runner scheduler.

### Separate lint and typecheck into same job
Saves one job slot.  
**Rejected:** mypy is significantly slower than ruff and has a different failure mode (pre-existing
errors in `web/routes/`). Keeping them separate allows the faster lint job to give quick feedback
without waiting for mypy.

### Non-blocking integration tests
Mark `test-integration` with `continue-on-error: true`.  
**Rejected:** acceptance criteria explicitly requires integration tests to be BLOCKING. The 30-min
timeout gives slow tests room to run without making the pipeline feel indefinite.

---

## Consequences

- All pushes to `master`/`main` and all PRs run quality gates automatically.
- Pre-existing issues at time of adoption:
  - **ruff:** 8 errors (7 auto-fixable) — `lint` job will fail until fixed.
  - **mypy:** 23 errors in 14 files — `typecheck` job will fail until fixed.
  - **import-linter:** all 4 contracts KEPT — passes.
  - **pytest unit:** coverage 91.93% — passes gate.
  - **async-sync-repo:** exits 0 — passes.
- The `lint` and `typecheck` failures are pre-existing technical debt; they are now **visible and
  blocking**, which is the intended effect.
- Coverage gate at 80% leaves room for `full_scan.py` and `lot_user_state.py` to be improved
  incrementally. Bump `--cov-fail-under` toward 90 once those files gain coverage.

---

## See also

- [[decisions/ADR-006-import-linter-ci|ADR-006]] — import-linter (first CI integration)
- [[decisions/ADR-041-test-tactics-amendment|ADR-041]] — test tactics, layer boundaries
- [[decisions/ADR-044-async-sync-repo-ast-check|ADR-044]] — async-sync-repo guard added as lint step
