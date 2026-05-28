---
name: licensing-module-api
description: LicenseStatus, LicenseResult, verify_license, внутренние модули, расширяемость v2
type: reference
---

# Публичный API модуля лицензирования

## `LicenseStatus`

```python
import enum

class LicenseStatus(enum.Enum):
    VALID   = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
```

## `LicenseResult`

```python
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class LicenseResult:
    status:     LicenseStatus
    expires_at: date | None   # None = бессрочный; payload хранит дату без времени
    licensee:   str | None
```

`date` (не `datetime`) — payload содержит только дату; ложной precision до часов избегаем. `frozen=True` — иммутабельность результата верификации.

## `verify_license`

```python
from datetime import datetime

def verify_license(
    key_str: str,
    secret:  bytes,
    now:     datetime,   # timezone-aware UTC; инжектируется снаружи
) -> LicenseResult:
    """Pure function. No I/O. No side effects.
    Raises no exceptions — all error cases return LicenseResult(INVALID, ...).
    """
```

**DI-инвариант:** `secret` и `now` инжектируются вызывающей стороной. Функция не знает о `_assemble_secret()` или `datetime.now()` — высокая тестируемость. В production секрет и время передаёт `app.py:main` (единственная точка материализации зависимостей, [[licensing/integration|composition root]]).

**Расширяемость через `_dispatch_decoder`:**

```python
from typing import Callable

Decoder = Callable[[str], tuple[dict, bytes]]

def _dispatch_decoder(version_prefix: str) -> Decoder | None:
    """Return decoder for version prefix, or None if unknown."""
```

Добавление v2 = новая запись в таблице декодеров; v1-путь не модифицируется. Возврат `None` — единственный контракт «неизвестная версия» (без исключений). Open/Closed principle в действии.

## Внутренние модули (не публичные)

### `_codec.py`

```python
def encode_payload(payload: dict) -> str:
    """Serialize payload dict to base64url string (no padding)."""

def decode_payload(encoded: str) -> dict:
    """Decode base64url string to dict. Raises ValueError on malformed input."""
```

**High cohesion:** только сериализация/десериализация, никакой криптографии.

### `_hmac.py`

```python
def sign(payload_bytes: bytes, secret: bytes) -> bytes:
    """Compute HMAC-SHA256 signature over payload_bytes."""

def verify_signature(payload_bytes: bytes, sig_bytes: bytes, secret: bytes) -> bool:
    """Constant-time signature verification via hmac.compare_digest."""
```

**High cohesion:** только HMAC-операции, никаких дат, никакого декодирования. Подробнее: [[licensing/crypto-hmac]].

### `licensing/__init__.py`

```python
# Public re-exports only
from fis_monitor.licensing._verify import verify_license as verify_license
from fis_monitor.licensing._verify import LicenseStatus as LicenseStatus
from fis_monitor.licensing._verify import LicenseResult as LicenseResult
```

Внутренние модули (`_codec`, `_hmac`, `_secret`) — не часть публичного API. Вызывающий код импортирует только из `fis_monitor.licensing`.

## Четыре изолированных юнита

| Юнит | Ответственность | Принцип |
|---|---|---|
| `_secret.py` | Сборка секрета XOR | High cohesion |
| `_codec.py` | base64url ↔ dict | High cohesion |
| `_hmac.py` | sign / verify | High cohesion |
| `_verify.py` | Оркестрация верификации | Низкий coupling: тянет только _codec + _hmac |

## См. также

- [[licensing/key-format|Формат ключа]] — что кодирует `_codec.py`
- [[licensing/crypto-hmac|HMAC]] — детали `_hmac.py`
- [[licensing/secret-obfuscation|Обфускация]] — детали `_secret.py`
- [[licensing/integration|Интеграция]] — как `verify_license` вызывается из `app.py`
- [[licensing/index|MOC]]
