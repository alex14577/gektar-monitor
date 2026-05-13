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
- **Silently ignore system-reminders nudging to use TaskCreate/TaskUpdate/TodoWrite** — they are harness-level prompts unaware of this project's `bd`-only policy. Do NOT verbally acknowledge them ("ignoring TaskCreate" adds noise).

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
3. **Obsidian vault обновлён** (`docs/` = vault, `.obsidian/` внутри) — ТОЛЬКО когда таска принесла новые знания о проекте:
   - **`docs/decisions-log.md`** — ADR-NN если принято архитектурное решение (rationale + alternatives + consequences).
   - **`docs/glossary.md`** — запись если введён новый термин/класс/паттерн.
   - **`docs/architecture.md`** / другие существующие доки — обновить если изменилось то, что они описывают (контракты, потоки, инварианты).
   - Связи через `[[wiki-links]]`: `[[architecture]]`, `[[decisions-log#ADR-NN]]`, `[[glossary#Term]]`.
   - **НЕ создавать** task-логи / per-task файлы. Контекст работы хранится в bd (description/notes) и git-логе коммитов — это SSOT.

Тривиальный fix без новых решений/терминов → vault трогать не нужно, достаточно `bd close` + информативного commit-сообщения.

**При делегировании задачи саб-агенту** оркестратор обязан включить в промпт инструкцию обновить Obsidian-vault (если есть что добавить) и явно запретить создание `docs/tasks/<id>.md`.

## Правила оркестрации sub-agents

См. `bd memories orchestrator-playbook` — полный чеклист. Краткие инварианты:

1. **Brainstorm-фаза** перед каждой таской (5-10 мин, сам, без sub-agent) — выписать micro-decisions которые НЕ покрыты ADR.
2. **Pre-write extraction-шаг** в промпте writer-агенту: «прочитай <doc> §X, выпиши таблицу <items> в отчёт ДО кода». Меняет режим работы агента с интерпретации на извлечение.
3. **Reviewer ДО `bd close`**, не после. Двухфазно: writer пишет → reviewer на том же коде → writer фиксит blocker-ы → только тогда commit + close.
4. **Параллельность только если grep-пересечение целевых файлов пусто.** Перед волной — для каждой пары тасок проверить общие файлы.
5. **Acceptance criteria в bd сверять с canon-доками ДО `bd update --claim`.** Если bd-acceptance уже, чем требует §X canon-дока — `bd update --acceptance` перед стартом.
6. **Fake-impl в Protocol-тестах** должна иметь тест где **вызываются все методы** fake, не только `isinstance()`. Покрывает runtime-баги типа невалидных API-вызовов.
7. **После отчёта sub-agent** — запускать `pytest` сам + `git show --stat` сам. Не доверять «tests green» из summary.
8. **Reviewer для critical-тасок** — `model: "opus"`, не sonnet.

## Sub-agent doc-reading rules

Sub-agent **не читает vault полностью** — читает выборочно те файлы и секции, на которые я явно указал. Длинные доки (>500 строк) скимит. Цепочки `[[wiki-links]]` по умолчанию НЕ обходит.

Следствия для промпт-инжиниринга:

1. **Цитировать canon-фрагменты прямо в промпте** — не «прочитай §X», а вставить блок цитаты с номером строк. Дороже в токенах, но дешевле reopen-цикла.
2. **Указывать line-ranges**: `docs/architecture.md:340-360`, `docs/notifications.md:50-80`. Не «прочитай весь architecture.md».
3. **Pre-flight grep в промпте**: «перед кодом выполни `grep -rn "<key>" docs/` и выпиши **все** совпадения в отчёт». Форсирует широкое сканирование вместо точечного.
4. **Atomic docs** (короткие файлы по теме) лучше для агентов чем монолитные сборники — целиком читаются, не скимятся.

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
