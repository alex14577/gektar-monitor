---
name: licensing-integration
description: Fail-closed flow в app.py:main, composition root, поведение при ошибках
type: architecture
---

# Интеграция лицензирования в app.py:main

## Fail-closed flow

```python
# 1. Вычислить anchor
anchor = Path(__file__).resolve()

# 2. Загрузить ключ
try:
    key_str = load_license_key(anchor)
except FileNotFoundError:
    print(
        f"ERROR: license.key not found. "
        f"Place a valid license.key next to the program "
        f"(expected: {anchor.parent.parent.parent / 'license.key'}).",
        file=sys.stderr,
    )
    sys.exit(1)

# 3. Верифицировать
result = verify_license(
    key_str,
    secret=_assemble_secret(),
    now=datetime.now(timezone.utc),
)

# 4. Fail-closed switch
match result.status:
    case LicenseStatus.VALID:
        pass  # продолжить нормальный запуск
    case LicenseStatus.EXPIRED:
        print(
            f"ERROR: License expired on {result.expires_at:%Y-%m-%d}. "
            f"Contact your vendor for renewal.",
            file=sys.stderr,
        )
        sys.exit(1)
    case LicenseStatus.INVALID:
        print(
            "ERROR: License is invalid. Check license.key contents.",
            file=sys.stderr,
        )
        sys.exit(1)
```

## Инварианты fail-closed

- Никакого retry, никакого grace-period, никакого интерактивного prompt
- `sys.exit(1)` при любом сбое лицензии — **до** инициализации остальных подсистем
- Сообщения об ошибках всегда в `stderr`; stdout остаётся чистым для пайпов
- Поведение при EXPIRED включает дату истечения для диагностики

## Composition root

`app.py:main` — **единственное место** в production-коде, где допускается прямой вызов `_assemble_secret()` и `datetime.now(timezone.utc)`. DI-инвариант из [[licensing/secret-obfuscation|secret-obfuscation]] («секрет инжектируется параметром») распространяется на `verify_license` и всё ниже.

`main()` как точка входа обязан где-то материализовать зависимости — это легитимно. Code reviewer **не должен** помечать вызов `_assemble_secret()` в `main()` как нарушение DI.

## Позиция проверки в lifecycle

Проверка лицензии выполняется **первым действием** `main()`, до:
- инициализации БД
- запуска background-задач
- старта веб-сервера

Это гарантирует: нелицензионный запуск не оставляет состояния.

## Импорты в app.py

```python
from pathlib import Path
from datetime import datetime, timezone
import sys

from fis_monitor._license_loader import load_license_key
from fis_monitor.licensing import verify_license, LicenseStatus
from fis_monitor.licensing._secret import _assemble_secret
```

`_assemble_secret` импортируется напрямую из `_secret` (приватный модуль), а не через публичный `__init__` — это intentional: генератор и `main()` — единственные легитимные потребители.

## См. также

- [[licensing/license-key-file|Загрузчик]] — `load_license_key`
- [[licensing/module-api|Публичный API]] — `verify_license`, `LicenseResult`
- [[licensing/secret-obfuscation|Обфускация]] — `_assemble_secret`
- [[licensing/architecture|Архитектура]] — полный поток запуска
- [[licensing/index|MOC]]
