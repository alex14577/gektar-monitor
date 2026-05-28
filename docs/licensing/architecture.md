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
    cli.py                ← CLI-генератор (entry point gektar-gen-license)
  app.py:main             ← composition root, fail-closed
```

CLI-генератор (`licensing/cli.py`) лежит внутри пакета и устанавливается как console script `gektar-gen-license` через `[project.scripts]` в `pyproject.toml`. PyInstaller-сборка end-user дистрибутива бундлит только импорт-граф `fis_monitor.app:main`, поэтому в распакованном релизе CLI всё равно недоступен — см. [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]].

**High cohesion:** каждый файл отвечает ровно за одно: кодек, HMAC, верификация, сборка секрета, загрузка файла. Изменение крипто-алгоритма не затрагивает кодек или загрузчик.

## Coupling-матрица

| Модуль | Зависит от |
|---|---|
| `_secret.py` | stdlib only |
| `_codec.py` | stdlib only (`base64`, `json`) |
| `_hmac.py` | stdlib only (`hmac`, `hashlib`) |
| `_verify.py` | `_codec`, `_hmac` — **никакого I/O, никакого `datetime.now()`** |
| `_license_loader.py` | stdlib only (`pathlib`) |
| `licensing/__init__` | только `_verify` (re-export) |
| `app.py` | `_license_loader`, `_secret`, `licensing` |
| `licensing/cli.py` | `_secret`, `_codec`, `_hmac`, stdlib |

**Low coupling** достигается тем, что `_verify.py` не тянет I/O и не знает о файлах — верификация полностью отделена от загрузки.

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
        ├─ нет префикса "v1."  ──► INVALID
        ├─ malformed base64    ──► INVALID
        ├─ malformed JSON      ──► INVALID
        ├─ подпись не совпала  ──► INVALID
        ├─ now < iat           ──► INVALID  (грубый откат часов)
        ├─ now > exp           ──► EXPIRED
        └─ всё ок              ──► VALID
             │
             ├─ VALID   ──► продолжить запуск
             ├─ EXPIRED ──► stderr + sys.exit(1)
             └─ INVALID ──► stderr + sys.exit(1)
```

**Composition root** — `app.py:main` — единственное место, где материализуются `_assemble_secret()` и `datetime.now(UTC)`. Всё ниже по стеку получает зависимости через параметры (DI).

## Расширяемость на v2

Диспатч по версии реализован через `_dispatch_decoder(version_prefix)`, возвращающий декодер или `None`. Добавление v2 = новая запись в таблице декодеров; v1-путь не модифицируется (Open/Closed principle).

## См. также

- [[licensing/index|MOC]] — все ноты подсистемы
- [[licensing/module-api|Публичный API]]
- [[licensing/integration|Интеграция в app.py]]
- [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]]
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]]
