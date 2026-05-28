---
name: ADR-057-licensing-cli-as-entry-point
description: Packaging gen_license как console_script gektar-gen-license внутри fis_monitor.licensing.cli
type: decision
---

# ADR-057 — Licensing CLI as console_script entry point

**Status:** Accepted
**Date:** 2026-05-28
**Supersedes (partial):** «Dev-only гарантия» в [[licensing/architecture|licensing-architecture]] и [[licensing/generator-cli|generator-cli]] (исходная редакция от 2026-05-28 первой итерации).
**Related:** [[ADR-056-licensing-hmac-stateless-offline|ADR-056]], [[ADR-026-distribution-packaging-pyinstaller|ADR-026]].

## Контекст

Первая итерация licensing (см. ADR-056) держала генератор ключей вне пакета:
`tools/gen_license.py`, запуск `python -m tools.gen_license`. Файл не входил в
wheel, обоснование — «dev-only, никогда не в дистрибутиве».

Это создало два практических неудобства:

1. **bd gektar_monitor-5bf6** требует интерактивный режим при двойном клике —
   значит CLI должен распространяться как именованный исполняемый файл
   (entry-point), а не как `python -m tools.gen_license` из чекаута репо.
2. После `pip install fis-monitor` команды генерации ключей нет в `PATH`,
   приходится клонировать репозиторий — добавочный шаг для оператора,
   выпускающего лицензии.

## Решение

Перенести модуль:
- из `tools/gen_license.py`
- в `src/fis_monitor/licensing/cli.py`

Зарегистрировать console_script в `pyproject.toml`:

```toml
[project.scripts]
gektar-gen-license = "fis_monitor.licensing.cli:main"
```

После `pip install -e .` или установки wheel-а команда `gektar-gen-license`
доступна в `PATH`.

## Почему это безопасно

Исходный аргумент «не упаковывать в дистрибутив» защищал от утечки CLI
конечному пользователю. Этот аргумент сохраняется в **PyInstaller-сборке**
(см. [[ADR-026-distribution-packaging-pyinstaller|ADR-026]]): PyInstaller
бундлит только импорт-граф от `fis_monitor.app:main`. Модуль
`fis_monitor.licensing.cli` в этот граф не входит — `app.py` импортирует
только `_license_loader`, `_secret`, `licensing.__init__` (re-export
`_verify`), но не `cli`. Распакованный `.exe` end-user-а не содержит
исполняемого CLI генератора.

Wheel, опубликованный для операторов лицензирования, содержит `cli.py` —
ровно тот аудиторный сегмент, которому нужен генератор.

**Секрет** уже шипится в обеих формах: `_secret.py` с `_P1`/`_P2`
обязателен для runtime-верификации и в wheel-е, и в PyInstaller-сборке.
Перенос CLI в пакет не увеличивает поверхность утечки секрета — она
определяется уровнем обфускации `_assemble_secret`, не местоположением
CLI.

## Гарды (введены вместе с переездом)

1. **`_SECRET_INITIALIZED: bool`** — флаг в `_secret.py`. CLI
   `gektar-gen-license init-secret` отказывается работать пока флаг True
   (exit 1, печать предупреждения). Намеренная ротация — через `--force`.
   Защита от случайного `init-secret`, который иначе тихо ротирует
   секрет и убивает все ключи.

2. **import-linter контракт `licensing-cli-not-in-app-graph`** —
   `fis_monitor.licensing.cli` запрещён к импорту из `fis_monitor.app`,
   `composition`, `web`, `services`, `infra`, `domain`. Гарантирует
   PyInstaller-инвариант на CI: добавить `import` где не следует — и
   `lint-imports` упадёт до релизной сборки.

## Изменения

- `tools/gen_license.py` — удалён.
- `src/fis_monitor/licensing/cli.py` — добавлен (логика идентична, `prog`
  обновлён на `gektar-gen-license`, добавлен guard `--force` для
  `init-secret`).
- `pyproject.toml` → `[project.scripts]` — добавлена строка
  `gektar-gen-license = "fis_monitor.licensing.cli:main"`.
- `src/fis_monitor/licensing/_secret.py` — добавлен флаг
  `_SECRET_INITIALIZED: bool = True`; docstring/комментарий упоминают
  `gektar-gen-license init-secret` вместо `python -m tools.gen_license init-secret`.
- `.importlinter` — добавлен контракт `licensing-cli-not-in-app-graph`.
- `docs/licensing/architecture.md` — coupling-матрица и дерево модулей
  обновлены; добавлена ссылка на этот ADR.
- `docs/licensing/generator-cli.md` — все примеры заменены на
  `gektar-gen-license`; раздел «Dev-only гарантия» переписан в «Распространение и
  dev-only гарантия».
- `docs/licensing/manual-smoke.md` — путь past-date guard указывает на
  `src/fis_monitor/licensing/cli.py`; команды смоука — `gektar-gen-license`.
- `docs/licensing/test-strategy.md` — модуль в таблице покрытия переименован.

## Альтернативы

1. **Оставить `tools/gen_license.py`, добавить как top-level пакет `tools` в
   `[tool.setuptools.packages.find]`.** Отвергнуто: `tools/` содержит и
   `fake_torgi/` (домен тестов), смешение dev-fixture-сервера и production-grade
   CLI лицензирования в одном top-level пакете — low cohesion. Чище перенести
   CLI к модулю, который он обслуживает.

2. **Сделать `gen_license` модулем `fis_monitor.gen_license` без подпакета
   `licensing`.** Отвергнуто: CLI логически принадлежит подсистеме
   `licensing` (импортирует `_codec`, `_hmac`, `_secret` оттуда). Иерархия
   должна отражать coupling.

3. **Wrapper-скрипт `bin/gektar-gen-license.py` + entry-point на него.**
   Отвергнуто: лишний уровень indirection без выгоды.

## Consequences

**Позитив:**
- `gektar_monitor-5bf6` (интерактивный режим, двойной клик) перестаёт быть
  заблокированным — теперь у CLI есть стабильное имя на диске.
- Оператор лицензирования: `pip install fis-monitor` + `gektar-gen-license
  issue …`, без клонирования репозитория.
- Соответствие пакетной иерархии: CLI рядом с модулями, которые он
  использует.

**Негатив / риски:**
- CLI попадает в wheel — wheel может оказаться в руках человека без
  понимания смысла команды `init-secret`. Митигация: runtime-guard
  `_SECRET_INITIALIZED` (требует `--force`); явное предупреждение в
  docstring `_assemble_secret`.
- PyInstaller-инвариант (cli не попадает в end-user сборку) хрупок —
  единственный лишний `import` может его сломать. Митигация:
  import-linter контракт `licensing-cli-not-in-app-graph` в CI.

## См. также

- [[licensing/architecture|licensing-architecture]]
- [[licensing/generator-cli|licensing-generator-cli]]
- [[ADR-056-licensing-hmac-stateless-offline|ADR-056]]
- [[ADR-026-distribution-packaging-pyinstaller|ADR-026]]
