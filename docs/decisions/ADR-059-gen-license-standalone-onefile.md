---
name: ADR-059-gen-license-standalone-onefile
description: PyInstaller --onefile для gektar-gen-license; отдельный venv build/.venv-gen; size gate 25 MB
type: decision
---

# ADR-059 — gektar-gen-license standalone --onefile distribution

**Status:** Accepted
**Date:** 2026-05-29
**Supersedes (partial):** [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — §onefile-restriction (применима только к fis-monitor с Chromium; для gen-license inapplicable)
**Related:** [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]], [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]], [[decisions/ADR-058-license-payload-v2|ADR-058]]

## Контекст

ADR-026 принял для fis-monitor режим **--onedir** и явно отверг --onefile:

> `--onefile` wraps everything in a single executable that extracts to `$TMPDIR`
> on every launch — adding 5-15 seconds of cold-start latency and requiring
> writable `/tmp`. Rejected.

Этот аргумент критичен для fis-monitor, где Playwright Chromium (~280 MB)
распаковывается во временную директорию при каждом запуске.

`gektar-gen-license` — CLI без runtime-ресурсов:
- Нет Chromium, нет шаблонов, нет файлов данных.
- Все зависимости — stdlib + фрагмент `fis_monitor.licensing.*` (crypto-модули
  без 3rd-party dep тяжелее `cryptography` или `pycryptodome`).
- Замороженный бинарь: **~15 MB** (в 18× меньше fis-monitor archive).
- Cold-start при извлечении: **<200 мс** — приемлемо для интерактивного CLI.

Дополнительный контекст:
- Утилита распространяется **авторизованным партнёрам-реселлерам**, не
  конечным пользователям. Один файл проще передавать (email, мессенджер,
  общий диск) и не требует поддерживать структуру директорий.
- Windows SmartScreen показывает предупреждение «неизвестный издатель» для
  неподписанных .exe — это документировано в README и приемлемо для
  небольшого числа доверенных партнёров.

## Решение

**PyInstaller `--onefile`** (`exclude_binaries=False`, нет COLLECT-блока)
для `build/gektar-gen-license.spec`.

| Параметр | Значение | Обоснование |
|---|---|---|
| Mode | `--onefile` | Единственный файл, cold-start <200 мс |
| UPX | `upx=False` | Нет измеримой пользы при ~15 MB; риск AV false-positive |
| Console | `console=True` | Обязателен для интерактивных stdio-запросов |
| Venv | `build/.venv-gen` | Изолирован от fis-monitor venv (`build/.venv`) |
| Size gate | <25 MB | Ранняя защита от случайного dependency bloat |

**Отдельный venv `build/.venv-gen`**:
- Предотвращает проникновение тяжёлых зависимостей fis-monitor
  (playwright, psutil, watchdog и т.д.) в бинарь gen-license.
- PyInstaller собирает граф импортов из venv; общий venv с fis-monitor
  мог бы включить дополнительные пакеты даже при явном `excludes`.

**Excludes в spec**: defence-in-depth дополнение к import-linter контракту
`gen-license-cli-no-app-graph` (см. ADR-057 аналог, обратный контракт в
`.importlinter`). Если будущий рефакторинг случайно добавит импорт в app-граф,
size gate в CI упадёт до релиза.

**Артефакты**:
- Linux: `dist/gektar-gen-license-linux-x86_64-<ver>` (одиночный файл)
- Windows: `dist/gektar-gen-license-windows-x86_64-<ver>.exe`
- SHA256 рядом с каждым артефактом.

**Без code signing**: SmartScreen warning документирован в README-gen-license.txt.
Для небольшой аудитории авторизованных партнёров это приемлемо. Подпись —
будущая задача при росте аудитории или корпоративных требованиях.

## Альтернативы

### onedir + zip-архив
- Ближе к fis-monitor подходу.
- Минус: партнёру нужно распаковывать архив и хранить директорию; передача
  одного файла удобнее для CLI-утилиты без состояния.
- Отвергнуто: UX хуже при тех же security-свойствах.

### wheel + `pip install`
- Чистое Python-решение, нет frozen binary.
- Минус: требует Python 3.12+ у партнёра-реселлера; добавляет зависимость
  от совместимости интерпретатора; партнёр должен управлять виртуальным окружением.
- Отвергнуто: frozen binary устраняет «у меня нет Python» класс проблем.

### Docker-образ
- Портабельность без Python у партнёра.
- Минус: требует Docker daemon; добавляет операционный overhead для CLI,
  который запускается несколько раз в месяц.
- Отвергнуто: непропорционально сложно для задачи.

### Общий venv с fis-monitor (build/.venv)
- Экономия места на диске при локальной разработке.
- Минус: playwright и другие зависимости fis-monitor попадают в граф
  PyInstaller и раздувают бинарь. Тест: playwright alone добавляет ~100 MB.
- Отвергнуто: нарушает size gate и принцип low coupling между артефактами.

## Consequences

**Позитив:**
- Партнёр получает один файл без установки зависимостей.
- Cold-start <200 мс — незаметен для интерактивного CLI.
- Size gate в build-скрипте перехватывает dependency bloat до релиза.
- Изолированный venv предотвращает загрязнение бинаря тяжёлыми deps.

**Негатив / риски:**
- Windows Defender SmartScreen предупреждение при первом запуске у каждого
  партнёра. Митигация: задокументировано в README-gen-license.txt.
- `$TMPDIR` должен быть записываемым (стандарт для Windows и Linux).
  Нестандартные hardened среды (no-execute tmpfs) не поддерживаются — это
  принято как out-of-scope для аудитории партнёров.
- Каждый запуск включает ~200 мс распаковки — приемлемо для CLI, который
  не запускается тысячи раз в секунду.

## Изменения файлов

- `build/gektar-gen-license.spec` — новый PyInstaller spec (--onefile)
- `scripts/build_gen_license.sh` — новый build script (Linux)
- `scripts/build_gen_license.ps1` — новый build script (Windows)
- `scripts/templates/README-gen-license.txt` — README для партнёров (RU)
- `docs/operations/release-build-gen-license.md` — runbook
- `.importlinter` — новый контракт `gen-license-cli-no-app-graph`
- `docs/licensing/architecture.md` — раздел «Distribution packaging»
- `docs/decisions-log.md` — запись ADR-059

## Enforcement

The size gate and import-graph isolation are enforced at two layers:

1. **import-linter** (`lint-imports --config .importlinter`, contract `gen-license-cli-no-app-graph`) — statically verifies that `fis_monitor.licensing.*` modules do not import app-layer packages. Run in CI via `.github/workflows/ci.yml` (lint job).
2. **Size gate in build script** (`scripts/build_gen_license.sh` / `.ps1`, step 5) — aborts with a non-zero exit if the frozen binary exceeds 25 MB, preventing accidental bloat from reaching CI artefacts.

Both checks are required: import-linter catches source-level violations before build; the size gate catches any PyInstaller hook or hidden import that bypasses static analysis.

## См. также

- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — fis-monitor --onedir, rationale против --onefile для Chromium
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]] — CLI как console_script, PyInstaller-инвариант
- [[decisions/ADR-058-license-payload-v2|ADR-058]] — payload v2, интерактивный режим CLI
- [[operations/release-build-gen-license|release-build-gen-license]] — runbook
