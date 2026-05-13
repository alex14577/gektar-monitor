# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
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

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
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
3. **Obsidian vault обновлён** (`docs/` = vault, `.obsidian/` внутри):
   - **`docs/tasks/<bd-id>.md`** — создан по шаблону из `docs/tasks/README.md` (что/почему/связи/follow-up). Обязательно для каждой закрытой таски.
   - **`docs/decisions-log.md`** — добавить ADR-NN если принято архитектурное решение (rationale + alternatives + consequences).
   - **`docs/glossary.md`** — добавить запись если ввели новый термин/класс/паттерн.
   - Связи между файлами через `[[wiki-links]]` (Obsidian-формат): `[[architecture]]`, `[[decisions-log#ADR-NN]]`, `[[glossary#Term]]`.

Тривиальный fix без новых решений/терминов → достаточно одного абзаца в `docs/tasks/<bd-id>.md`.

**При делегировании задачи саб-агенту** оркестратор обязан включить в промпт инструкцию заполнить Obsidian как часть DoD и указать какие файлы трогать.

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
