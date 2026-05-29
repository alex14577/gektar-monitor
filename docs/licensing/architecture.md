---
name: licensing-architecture
description: Модули подсистемы лицензирования, coupling-матрица, поток запуска
type: architecture
---

# Licensing — архитектура модулей

## Структура модулей

```
src/fis_monitor/
  _license_loader.py      ← load_license_key(anchor)
  licensing/
    __init__.py           ← публичный re-export
    _secret.py            ← _assemble_secret() — XOR-сборка секрета
    _codec.py             ← encode_payload / decode_payload
    _hmac.py              ← sign / verify_signature
    _verify.py            ← verify_license (чистая функция)
    _prompt.py            ← Prompter Protocol + ConsolePrompter (IO-boundary)
    _interactive.py       ← run_interactive(...) — application logic, DI
    cli.py                ← CLI-генератор (entry point gektar-gen-license)
  app.py:main             ← composition root, fail-closed
```

CLI-генератор (`licensing/cli.py`) лежит внутри пакета и устанавливается как console script `gektar-gen-license` через `[project.scripts]` в `pyproject.toml`. PyInstaller-сборка end-user дистрибутива бундлит только импорт-граф `fis_monitor.app:main`, поэтому в распакованном релизе CLI всё равно недоступен — см. [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]].

**High cohesion:** каждый файл отвечает ровно за одно: кодек, HMAC, верификация, сборка секрета, загрузка файла, I/O-абстракция, интерактивная логика. Изменение крипто-алгоритма не затрагивает кодек или загрузчик.

## Coupling-матрица

| Модуль | Зависит от |
|---|---|
| `_secret.py` | stdlib only |
| `_codec.py` | stdlib only (`base64`, `json`) |
| `_hmac.py` | stdlib only (`hmac`, `hashlib`) |
| `_verify.py` | `_codec`, `_hmac` — **никакого I/O, никакого `datetime.now()`** |
| `_prompt.py` | stdlib only (Protocol, input/print) |
| `_interactive.py` | `_prompt` (Protocol), stdlib (`sys`, `pathlib`, `datetime`) — никакого бизнес-кода дат |
| `_license_loader.py` | stdlib only (`pathlib`) |
| `licensing/__init__` | только `_verify` (re-export) |
| `app.py` | `_license_loader`, `_secret`, `licensing` |
| `licensing/cli.py` | `_secret`, `_codec`, `_hmac`, `_prompt`, `_interactive`, stdlib |

**Low coupling** достигается тем, что `_verify.py` не тянет I/O и не знает о файлах — верификация полностью отделена от загрузки. `_interactive.py` не импортирует `_secret` напрямую — он получает `secret_fn: Callable[[], bytes]` через DI.

## Поток при запуске программы

```
app.py:main
  │
  ├─► load_license_key(anchor)
  │     FileNotFoundError ──► stderr + sys.exit(1)
  │
  ├─► _assemble_secret()        ← единственный вызов в production
  │
  └─► verify_license(key_str, secret, now)
        │
        ├─ не "v2." префикс    ──► INVALID (unsupported version, incl. v1)
        ├─ malformed base64    ──► INVALID
        ├─ malformed JSON      ──► INVALID
        ├─ подпись не совпала  ──► INVALID
        ├─ now < nbf           ──► INVALID  (nbf-floor anti-rollback)
        ├─ now > exp           ──► EXPIRED
        └─ всё ок              ──► VALID
             │
             ├─ VALID   ──► продолжить запуск
             ├─ EXPIRED ──► stderr + sys.exit(1)
             └─ INVALID ──► stderr + sys.exit(1)
```

## Поток интерактивного режима CLI

```
gektar-gen-license (no args)
  │
  └─► cli.main() → _run_interactive_mode(ConsolePrompter())
        │
        └─► run_interactive(prompter, key_writer, builder, default_dir_fn, secret_fn)
              │
              ├─► ask nbf (retry on ValueError)
              ├─► ask exp (retry on ValueError or exp < nbf)
              ├─► ask dir (retry on non-existent; ask overwrite if license.key exists)
              ├─► builder(nbf, exp, secret_fn()) → key_str
              └─► key_writer(dir/license.key, key_str)
                    OSError ──► error + return 1
                    ok      ──► info + return 0
```

**Composition root** — `app.py:main` — единственное место, где материализуются `_assemble_secret()` и `datetime.now(UTC)`. Всё ниже по стеку получает зависимости через параметры (DI).

## Distribution packaging

### gektar-gen-license standalone .exe

CLI-генератор распространяется авторизованным партнёрам-реселлерам в виде
единственного исполняемого файла, собранного через PyInstaller `--onefile`.

**Артефакты:**
- `dist/gektar-gen-license-linux-x86_64-<ver>` (Linux, единственный файл)
- `dist/gektar-gen-license-windows-x86_64-<ver>.exe` (Windows)
- SHA256 рядом с каждым артефактом

**Сборка:**

```bash
bash scripts/build_gen_license.sh          # Linux
.\scripts\build_gen_license.ps1            # Windows
```

**Ключевые свойства:**
- Отдельный venv `build/.venv-gen` — изолирован от fis-monitor venv
  (`build/.venv`), предотвращает bloat через транзитивные зависимости.
- Size gate <25 MB в build-скрипте — ранняя защита от случайного подтягивания
  тяжёлых deps (playwright, fastapi и т.д.).
- Import-linter контракт `gen-license-cli-no-app-graph` запрещает `cli.py`,
  `_interactive.py`, `_prompt.py` импортировать app-layer и 3rd-party runtime-deps.
- `console=True`, `upx=False`, без code signing (SmartScreen warning).

**Обоснование --onefile:** ADR-026 отвергает --onefile для fis-monitor из-за
cold-start при распаковке Playwright Chromium (~280 MB). Для gen-license
(~15 MB, без Chromium) cold-start <200 мс — приемлемо для интерактивного CLI.
Подробнее: [[decisions/ADR-059-gen-license-standalone-onefile|ADR-059]].

## Расширяемость на v3

Если потребуется v3 payload: добавить `_decode_v3`, расширить `verify_license` аналогично — v2-путь не модифицируется (Open/Closed principle).

## См. также

- [[licensing/index|MOC]] — все ноты подсистемы
- [[licensing/module-api|Публичный API]]
- [[licensing/integration|Интеграция в app.py]]
- [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]]
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]]
- [[decisions/ADR-058-license-payload-v2|ADR-058]]
