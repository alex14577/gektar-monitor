---
name: licensing-license-key-file
description: Расположение файла license.key, формат, публичный API загрузчика
type: reference
---

# Файл license.key — расположение, формат, загрузчик

## Расположение файла

Фиксированный путь относительно `__file__` приложения. Без env-vars, без `platformdirs`.

```python
def _default_license_path(anchor: Path) -> Path:
    # anchor = Path(__file__).resolve() из app.py
    # src/fis_monitor/app.py
    #   → parent           = src/fis_monitor/
    #   → parent.parent    = src/
    #   → parent.parent.parent = project root
    return anchor.parent.parent.parent / "license.key"
```

Путь работает в двух контекстах:

| Контекст | Где лежит `license.key` |
|---|---|
| Dev (src-layout) | корень проекта, рядом с `pyproject.toml` |
| PyInstaller `--onedir` | корень распакованного каталога, рядом с исполняемым файлом |

## Формат файла

- Одна строка: `v1.<base64url_payload>.<base64url_sig>`
- Кодировка: UTF-8
- Никакого BOM, никаких метаданных
- При чтении применяется `.strip()` (обрезка пробелов и `\n`)

## Публичный API загрузчика

```python
def load_license_key(anchor: Path) -> str:
    """Read license key string from license.key next to the program.

    Args:
        anchor: Path to the calling module (__file__ resolved).

    Returns:
        Stripped key string.

    Raises:
        FileNotFoundError: if license.key does not exist.
    """
```

**Ответственность загрузчика:** только чтение строки. Валидация содержимого — задача `verify_license` ([[licensing/module-api|публичный API]]).

**Low coupling:** `_license_loader.py` зависит только от stdlib `pathlib`. Он не знает ни о криптографии, ни о формате ключа.

## Обработка ошибок при загрузке

`FileNotFoundError` перехватывается в `app.py:main` и превращается в информативное stderr-сообщение:

```
ERROR: license.key not found.
Place a valid license.key next to the program
(expected: /path/to/license.key).
```

Затем `sys.exit(1)`. Никакого grace-period, никакого retry. Подробнее: [[licensing/integration|fail-closed flow]].

## Как поставить ключ

1. Выпустить ключ: `python -m tools.gen_license issue ...` ([[licensing/generator-cli]])
2. Положить файл `license.key` рядом с программой
3. Запустить программу

## См. также

- [[licensing/integration|Интеграция в app.py]] — как `load_license_key` вызывается
- [[licensing/generator-cli|CLI-генератор]] — как выпускается ключ
- [[licensing/index|MOC]]
