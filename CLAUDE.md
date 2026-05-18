# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

## Definition of Done (per bd task)

Задача считается **DONE** только если выполнено ВСЁ:

1. Тесты зелёные, `ruff check` чистый, код закоммичен
2. `bd close <id>`
3. **Obsidian vault обновлён** (`docs/` = vault, `.obsidian/` внутри) — ТОЛЬКО когда таска принесла новые знания о проекте:
   - **`docs/decisions/ADR-NNN-<slug>.md`** — новый ADR-файл если принято архитектурное решение (rationale + alternatives + consequences). Затем добавить ссылку в `docs/decisions-log.md` (stub-MOC).
   - **`docs/glossary.md`** — запись если введён новый термин/класс/паттерн.
   - **`docs/architecture/<NN-section>.md`** / **`docs/data-model/<topic>.md`** / другие существующие доки — обновить если изменилось то, что они описывают (контракты, потоки, инварианты).
   - Связи через `[[wiki-links]]`: `[[architecture/03-protocols]]`, `[[decisions/ADR-NNN-<slug>|ADR-NNN]]`, `[[glossary#Term]]`.
   - **НЕ создавать** task-логи / per-task файлы. Контекст работы хранится в bd (description/notes) и git-логе коммитов — это SSOT.

Тривиальный fix без новых решений/терминов → vault трогать не нужно, достаточно `bd close` + информативного commit-сообщения.

**При делегировании задачи саб-агенту** оркестратор обязан включить в промпт инструкцию обновить Obsidian-vault (если есть что добавить) и явно запретить создание `docs/tasks/<id>.md`.

## Sub-agent orchestration & doc-reading

Detailed playbook lives in **`docs/agent-conventions.md`** (bd hve —
extracted from this file to keep the per-session preamble light). Read
that file before you dispatch sub-agents or before you yourself act as
one. Short pointers below; the canonical text is in agent-conventions.md.

- Brainstorm-фаза перед каждой таской (orchestrator, без sub-agent).
- Reviewer runs **before** `bd close`, not after. Writer fixes blockers,
  then commit + close.
- Sub-agents do not chase wiki-links — quote canon fragments inline,
  name atomic files, force pre-flight greps in the prompt.
- Parallel sub-agents only when their target-file sets do not intersect.
- Acceptance criteria in bd reconciled with canon docs BEFORE
  `bd update --claim` (use `bd update --acceptance` if narrower).
- Fake-impls in Protocol tests must have at least one test that invokes
  every method, not just `isinstance(...)`.
- After every sub-agent report: orchestrator personally re-runs
  `pytest` and `git show --stat`. Never trust «tests green» summaries.
- Critical-path reviews (security, schema, money/auth) use
  `model: "opus"`.

## Build & Test

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
