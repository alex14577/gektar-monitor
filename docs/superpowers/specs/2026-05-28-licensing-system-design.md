# Licensing System — Design Spec (v1)

> **bd epic:** `gektar_monitor-5yvb`
> **Date:** 2026-05-28
> **Status:** Approved (brainstorm complete)

---

## 1. Goal & Scope

Реализовать минимальную систему локального лицензирования для `fis-monitor` на основе активационных ключей. Ключ содержит зашитый абсолютный срок действия и HMAC-SHA256 подпись. Проверка выполняется stateless, полностью offline, без сервера и без привязки к железу.

**Целевой уровень сложности:** 2/10 (явное требование заказчика — «не делать сложно»).

**Scope:**
- Генератор ключей (dev-CLI, `tools/gen_license.py`)
- Встроенный модуль верификации (`src/fis_monitor/licensing/`)
- Загрузчик файла лицензии (`src/fis_monitor/_license_loader.py`)
- Интеграция fail-closed в точку входа (`src/fis_monitor/app.py:main`)
- Файл лицензии `license.key` рядом с программой

---

## 2. Constraints & Tradeoffs

### Явные ограничения

| Constraint | Решение |
|---|---|
| Без внешних крипто-зависимостей | Только stdlib: `hmac`, `hashlib`, `base64`, `json`, `secrets` |
| Без сервера | Проверка полностью локальная |
| Без привязки к железу | Machine ID, MAC-адрес, диск — вне scope |
| Без сложного UX | Fail-closed, stderr + exit(1), никакого интерактивного prompt |

### Honest security tradeoff

XOR-обфускация секрета в бинаре (см. §6) блокирует `strings`-атаку, но **не защищает от дизассемблера или отладчика**. Атакующий с достаточной мотивацией извлечёт секрет из бинаря и сможет генерировать произвольные действительные ключи без механизма отзыва. Это осознанный **security-through-obscurity** — принятый явно ради потолка сложности 2/10. Альтернативы (KMS, HSM, онлайн-валидация, привязка к железу) намеренно вынесены в Out of Scope.

### Clock trust

Защита от **тонкого** отката системных часов (например, на несколько часов) невозможна без persistent state или hardware. Используется `iat`-floor (см. §5): `now < iat` → INVALID. Это блокирует только грубый откат назад (дни/годы). Система явно **доверяет системным часам**.

---

## 3. Architecture Overview

### Модули и зависимости

```
tools/
  gen_license.py
    │
    ├── imports ──────────────────────────────────────────────────────┐
    │                                                                 │
    ▼                                                                 │
src/fis_monitor/                                                      │
  _license_loader.py          (load_license_key)                      │
  licensing/                                                          │
    __init__.py  ←── public re-export                                 │
    _secret.py   ←────────────────────────────────────────────────────┘
    _codec.py    (encode_payload / decode_payload)
    _hmac.py     (sign / verify_signature)
    _verify.py   (verify_license — чистая функция)
  app.py:main
    │
    ├── _license_loader.load_license_key(anchor)
    ├── _secret._assemble_secret()
    └── licensing.verify_license(key_str, secret, now)
```

### Поток при запуске программы

```
app.py:main
     │
     ▼
load_license_key(anchor)
     │ FileNotFoundError ──► stderr + sys.exit(1)
     │
     ▼ key_str
verify_license(key_str, secret, now)
     │
     ├─ decode prefix ──── "v1." missing ──────────────────► INVALID
     ├─ base64url decode ── malformed ─────────────────────► INVALID
     ├─ JSON parse ──────── malformed ─────────────────────► INVALID
     ├─ verify_signature ── mismatch ──────────────────────► INVALID
     ├─ now < iat ──────────────────────────────────────────► INVALID
     ├─ now > exp ──────────────────────────────────────────► EXPIRED
     └─ all checks pass ───────────────────────────────────► VALID
          │
          ▼
     LicenseResult(status, expires_at, licensee)
          │
          ├─ VALID   ──► continue startup
          ├─ EXPIRED ──► stderr + sys.exit(1)
          └─ INVALID ──► stderr + sys.exit(1)
```

### Зависимости между модулями (coupling matrix)

```
Модуль              Зависит от
─────────────────── ────────────────────────────────────────────────
_secret.py          stdlib only
_codec.py           stdlib only (base64, json)
_hmac.py            stdlib only (hmac, hashlib)
_verify.py          _codec, _hmac  (NO I/O, NO time.now())
_license_loader.py  stdlib only (pathlib)
licensing/__init__  _verify (re-export)
app.py              _license_loader, _secret, licensing
gen_license.py      _secret, _codec, _hmac, stdlib (argparse, datetime)
```

---

## 4. Key Format & Payload

### Строковый формат ключа

```
v1.<base64url_payload>.<base64url_sig>
```

- Одна строка, UTF-8
- Разделитель: `.` (точка)
- `base64url` — alphabet `A-Za-z0-9-_`, **без padding** (`=` отсутствуют)
- При декоде padding восстанавливается: `s += '=' * (-len(s) % 4)`
- Первый токен — литерал `v1` (идентификатор версии формата)

### Поля payload

| Поле | Тип | Обязательное | Описание |
|---|---|---|---|
| `v` | int | да | Версия формата; для v1 = `1` |
| `iat` | string | да | ISO-8601 UTC дата выпуска (`YYYY-MM-DD`); anti-rollback floor |
| `exp` | string \| null | нет | ISO-8601 UTC дата истечения (`YYYY-MM-DD`); отсутствие или `null` = бессрочный |
| `lic` | string | да | Идентификатор получателя (произвольная строка) |

### Канонический JSON (детерминированный)

```python
json.dumps(payload_dict, sort_keys=True, separators=(',', ':'))
```

`sort_keys=True` гарантирует стабильный порядок ключей — сигнируется именно эта каноническая форма. Никаких пробелов (`separators=(',', ':')`) — минимальный размер.

### Пример payload (до base64url)

```json
{"exp":"2026-12-31","iat":"2026-05-28","lic":"Acme Corp","v":1}
```

### Пример полного ключа (структурный, не реальный)

```
v1.eyJleHAiOiIyMDI2LTEyLTMxIiwiaWF0IjoiMjAyNi0wNS0yOCIsImxpYyI6IkFjbWUgQ29ycCIsInYiOjF9.W3NpZ25hdHVyZV9ieXRlc19oZXJlXQ
```

---

## 5. Crypto: HMAC-SHA256

### Подпись

```
sig = hmac.new(key=secret, msg=payload_bytes, digestmod=hashlib.sha256).digest()
```

Где `payload_bytes` — UTF-8 байты канонического JSON. Именованные аргументы обязательны в коде (явная защита от перепутанного позиционного порядка).

### Верификация

```
expected = hmac.new(key=secret, msg=payload_bytes, digestmod=hashlib.sha256).digest()
ok = hmac.compare_digest(expected, received_sig_bytes)
```

**Blocker-правило для code review:** использовать `hmac.compare_digest`, **не** `==`. Сравнение `==` уязвимо к timing-атаке и является blocker-дефектом при ревью.

### Парсинг и сравнение дат (обязательный контракт)

`iat` и `exp` в payload — строки `YYYY-MM-DD`. Сравнение с `now: datetime` (timezone-aware UTC) выполняется **только по дате**, без часов/минут:

```python
from datetime import date

iat_date = date.fromisoformat(payload["iat"])
exp_raw  = payload.get("exp")   # None или отсутствует → бессрочный
exp_date = date.fromisoformat(exp_raw) if exp_raw else None

today = now.date()              # now — datetime(UTC), берём только дату

if today < iat_date:                       # грубый откат часов
    return LicenseResult(INVALID, ...)
if exp_date is not None and today > exp_date:
    return LicenseResult(EXPIRED, expires_at=exp_date, ...)
return LicenseResult(VALID, expires_at=exp_date, ...)
```

**Граничные случаи (зафиксированы):**
- `today == exp_date` → **VALID** (последний день валидности включён)
- `today == iat_date` → **VALID** (день выпуска включён)
- `exp` отсутствует ИЛИ `null` → бессрочный (никогда не EXPIRED)

### Статусы верификации

```
VALID    — подпись корректна, today <= exp_date (или exp отсутствует), today >= iat_date
EXPIRED  — подпись корректна, но today > exp_date
INVALID  — всё остальное: битая подпись, malformed ключ, today < iat_date,
           неизвестный префикс версии
```

### Anti-rollback через `iat`

При проверке: если `today < iat_date` → `INVALID`. Тривиальная однострочная защита от грубого отката системных часов назад. Тонкий откат (часы, минуты) — см. §2 «Clock trust».

---

## 6. Secret Obfuscation (XOR)

### Мотивация

Хранить секрет строкой-константой — значит найти его через `strings` на бинаре за секунды. XOR-сборка из двух частей убирает опознаваемую константу. Атака через дизассемблер всё равно сработает (см. §2 Honest security tradeoff) — это не претендует на полную защиту.

### Структура `_secret.py`

```python
# src/fis_monitor/licensing/_secret.py

def _assemble_secret() -> bytes:
    """Assembles the HMAC secret at runtime from two XOR parts.

    Neither _P1 nor _P2 alone is the secret.
    strings(1) will not find the full secret in the binary.
    """
    _P1: bytes = b'\x3f\xa1\x...'  # 32 bytes, opaque
    _P2: bytes = b'\x51\xd4\x...'  # 32 bytes, opaque
    return bytes(a ^ b for a, b in zip(_P1, _P2))
```

**Инварианты:**
- `len(_P1) == len(_P2) == 32`
- Функция — чистая, без side-effects, без I/O
- `_P1`, `_P2` — локальные переменные внутри функции, не модульные константы
- Результирующий секрет: 32 байта = 256 бит

### Как инициализировать секрет (одноразово)

CLI-команда `init-secret` (см. §9) генерирует случайный 32-байтный секрет, разбивает его XOR-ом на две части и печатает готовые Python-литералы для копипасты в `_secret.py`. Команда **не перезаписывает файл автоматически** — только печатает в stdout. Это устраняет риск случайной ротации.

### Dependency Injection секрета

Функция `verify_license` и `gen_license.py` не вызывают `_assemble_secret()` изнутри. Секрет передаётся параметром. Это позволяет тестам подставлять произвольный тестовый секрет, не зная значений `_P1`/`_P2`.

```python
# В app.py:main — единственное место вызова в production-коде
result = verify_license(key_str, secret=_assemble_secret(), now=datetime.now(timezone.utc))
```

---

## 7. Module API

### `LicenseStatus`

```python
import enum

class LicenseStatus(enum.Enum):
    VALID   = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
```

### `LicenseResult`

```python
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class LicenseResult:
    status:     LicenseStatus
    expires_at: date | None      # None = бессрочный; payload хранит дату без времени
    licensee:   str | None
```

Тип `date` (не `datetime`) — payload содержит только дату; ложной precision до часов/секунд избегаем.

### `verify_license`

```python
from datetime import datetime

def verify_license(
    key_str: str,
    secret:  bytes,
    now:     datetime,         # timezone-aware UTC; инжектируется снаружи
) -> LicenseResult:
    """Verify a license key string.

    Pure function. No I/O. No side effects.
    Raises no exceptions — all error cases return LicenseResult(INVALID, ...).

    Args:
        key_str: Full key string, e.g. "v1.<payload>.<sig>"
        secret:  32-byte HMAC secret, injected by caller
        now:     Current UTC datetime, injected by caller (testability)

    Returns:
        LicenseResult with status VALID, EXPIRED, or INVALID.
    """
```

### Внутренние функции (не публичные, но задокументированы для writer-агента)

#### `_codec.py`

```python
def encode_payload(payload: dict) -> str:
    """Serialize payload dict to base64url string (no padding)."""

def decode_payload(encoded: str) -> dict:
    """Decode base64url string to dict. Raises ValueError on malformed input."""
```

#### `_hmac.py`

```python
def sign(payload_bytes: bytes, secret: bytes) -> bytes:
    """Compute HMAC-SHA256 signature over payload_bytes."""

def verify_signature(payload_bytes: bytes, sig_bytes: bytes, secret: bytes) -> bool:
    """Constant-time signature verification via hmac.compare_digest."""
```

#### `_verify.py`

```python
def verify_license(key_str: str, secret: bytes, now: datetime) -> LicenseResult:
    """See public API above."""
```

#### `licensing/__init__.py`

```python
# Public re-exports only
from fis_monitor.licensing._verify import verify_license as verify_license
from fis_monitor.licensing._verify import LicenseStatus as LicenseStatus
from fis_monitor.licensing._verify import LicenseResult as LicenseResult
```

### Расширяемость на v2

Декодер первым проверяет строковый префикс (`v1.`). Логика диспатча по версии реализована отдельной чистой функцией:

```python
from typing import Callable

# Сигнатура декодера: (без-префиксная часть ключа) -> (payload_dict, sig_bytes)
Decoder = Callable[[str], tuple[dict, bytes]]

def _dispatch_decoder(version_prefix: str) -> Decoder | None:
    """Return decoder for a given version prefix, or None if unknown.

    Unknown version → caller returns LicenseResult(INVALID, ...).
    Adding v2 = add new entry in the registry; v1 code is not modified.
    """
```

Добавление v2 = новая запись в таблице декодеров без модификации v1-пути (Open/Closed). Возврат `None` — единственный контракт «неизвестная версия» (исключения не используются).

---

## 8. license.key — File Location, Format, Reader

### Расположение файла

Фиксированный путь относительно `__file__` приложения. Без env-vars, без platformdirs.

**Алгоритм в `_license_loader.py`:**

```python
def _default_license_path(anchor: Path) -> Path:
    # anchor = Path(__file__).resolve() из app.py
    # src/fis_monitor/app.py → parent = src/fis_monitor/
    #                        → parent.parent = src/
    #                        → parent.parent.parent = project root
    return anchor.parent.parent.parent / "license.key"
```

Путь работает одинаково:
- В dev-режиме (src-layout): корень проекта (рядом с `pyproject.toml`)
- В PyInstaller `--onedir`: корень распакованного каталога (рядом с исполняемым файлом)

### Формат файла

- Одна строка: `v1.<base64url_payload>.<base64url_sig>`
- Кодировка: UTF-8
- При чтении применяется `.strip()` (обрезка пробельных символов и `\n`)
- Никакого BOM, никаких метаданных

### Публичный API загрузчика

```python
def load_license_key(anchor: Path) -> str:
    """Read license key string from license.key next to the program.

    Args:
        anchor: Path to the calling module (__file__ resolved).

    Returns:
        Stripped key string.

    Raises:
        FileNotFoundError: if license.key does not exist at computed path.
    """
```

Функция не интерпретирует содержимое — только читает строку. Валидация — задача `verify_license`.

---

## 9. Generator CLI

### Запуск

```
python -m tools.gen_license <command> [options]
```

CLI построен на `argparse` (stdlib). Click не используется.

### Команда `init-secret`

```
python -m tools.gen_license init-secret
```

**Поведение:**
1. Генерирует `secrets.token_bytes(32)` — случайный 32-байтный секрет
2. Разбивает XOR-ом на две части: `_P1 = random_bytes(32)`, `_P2 = secret XOR _P1`
3. Печатает в stdout готовые Python-литералы для вставки в `_secret.py`:

```
_P1 = b'\x3f\xa1\x...'
_P2 = b'\x51\xd4\x...'
```

4. Явно не перезаписывает `_secret.py` — только печатает. Ручная вставка — намеренно.

**Когда использовать:** один раз при первоначальной настройке проекта. Повторный запуск сгенерирует новый секрет и сломает все ранее выпущенные ключи.

### Команда `issue`

```
python -m tools.gen_license issue \
    (--duration day|week|month|forever | --expires YYYY-MM-DD) \
    --licensee NAME \
    [--out FILE]
```

**Флаги:**

| Флаг | Описание |
|---|---|
| `--duration day\|week\|month\|forever` | Вычислить `exp` от сегодняшней UTC-даты |
| `--expires YYYY-MM-DD` | Задать `exp` явно |
| `--duration` и `--expires` | Взаимоисключающие, ровно один обязателен: `add_mutually_exclusive_group(required=True)` |
| `--licensee NAME` | Обязательный; строка идентификатора получателя |
| `--out FILE` | Записать ключ в файл; по умолчанию — stdout |

**`--duration forever`:** поле `exp` отсутствует в payload (бессрочный ключ).

**Пример:**
```
python -m tools.gen_license issue --duration month --licensee "Acme Corp" --out license.key
```

**Источник секрета:** `gen_license.py` импортирует `_assemble_secret` из `fis_monitor.licensing._secret`. Единый источник правды — нет риска рассинхрона генератор↔верификатор.

**Dev-only гарантия:** `tools/gen_license.py` — **dev-инструмент, никогда не упаковывается в дистрибутив**. PyInstaller бундлит только модули, импортируемые из entry-point `fis_monitor.app:main`; `tools/` в этом графе не участвует. Поэтому импорт приватного `_assemble_secret` из `fis_monitor.licensing._secret` — допустимая dev-зависимость, не нарушение публичного API. Это явный контракт: запускается из репозитория разработчика, не из распакованного релиза.

---

## 10. Integration into app.py:main (Fail-Closed)

Полный fail-closed flow в `main()`:

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

**Инварианты:**
- Никакого retry, никакого grace-period, никакого интерактивного prompt
- `sys.exit(1)` при любом сбое лицензии — до инициализации остальных подсистем
- Сообщение об ошибке всегда в `stderr`; stdout остаётся чистым для пайпов

**Composition root:** `app.py:main` — **единственное** место в production-коде, где допускается прямой вызов `_assemble_secret()` и `datetime.now(timezone.utc)`. DI-инвариант из §6 («секрет инжектируется параметром») распространяется на `verify_license` и всё, что под ним; `main()` как точка входа обязан где-то материализовать зависимости — это легитимно и не нарушает DI-контракт. Code reviewer не должен помечать вызов `_assemble_secret()` в `main()` как нарушение.

---

## 11. Test Strategy

### Принцип: тесты на МОДУЛЬ через публичный контракт

Юнит-тесты пишутся на **модули** через их публичную поверхность, **не** на каждую внутреннюю функцию. Внутренние функции (`encode_payload`, `decode_payload`, `sign`, `verify_signature`, `_dispatch_decoder`) **не тестируются напрямую** — они exercised транзитивно через тесты публичного API.

**Что это даёт:**
- ~10× меньше тестов при том же покрытии инвариантов
- Свобода рефакторинга внутренностей без переписывания тестов
- Тесты документируют ПОВЕДЕНИЕ системы, не структуру кода

**Что это означает на практике:**
Если для VALID-ключа `verify_license` возвращает `VALID` — значит и кодек, и HMAC, и парсинг дат, и dispatch версии работают. Если для tampered-ключа возвращает `INVALID` — значит `compare_digest` и подпись работают. Дублировать это в отдельных `test_codec.py`/`test_hmac.py` — over-test.

### Модули с публичной поверхностью (тестируем автоматически)

| Модуль | Публичная функция | Тестовый файл |
|---|---|---|
| `licensing` (фасад) | `verify_license(key_str, secret, now) -> LicenseResult` | `tests/licensing/test_verify_license.py` |
| `_license_loader` | `load_license_key(anchor) -> str` | `tests/licensing/test_license_loader.py` |
| `_secret` | `_assemble_secret() -> bytes` | `tests/licensing/test_secret_smoke.py` |

**Эти три файла — ВСЁ автотестовое покрытие подсистемы.** Никаких `test_codec.py`, `test_hmac.py`, `test_verify.py` отдельно.

### `test_verify_license.py` — параметризованный (~10 кейсов)

Один файл с `pytest.mark.parametrize`. Матрица покрывает все инварианты §5:

| # | Кейс | Ожидание |
|---|---|---|
| 1 | VALID: today между iat и exp | `status=VALID, expires_at=date` |
| 2 | VALID: бессрочный (нет `exp`) | `status=VALID, expires_at=None` |
| 3 | VALID: today == exp_date (граница) | `status=VALID` |
| 4 | VALID: today == iat_date (граница) | `status=VALID` |
| 5 | EXPIRED: today > exp_date | `status=EXPIRED, expires_at=date` |
| 6 | INVALID: today < iat_date (откат часов) | `status=INVALID` |
| 7 | INVALID: tampered payload | `status=INVALID` |
| 8 | INVALID: tampered signature | `status=INVALID` |
| 9 | INVALID: malformed base64 | `status=INVALID` |
| 10 | INVALID: неизвестный префикс версии (не `v1.`) | `status=INVALID` |

Плюс отдельный smoke-тест чистоты: два вызова `verify_license` с одинаковыми (key, secret, now) дают одинаковый результат → DI выдержано, нет скрытого `datetime.now()` внутри.

### `test_license_loader.py` — 3 кейса

| # | Кейс | Ожидание |
|---|---|---|
| 1 | Валидный файл → stripped строка | OK |
| 2 | Файл отсутствует | `FileNotFoundError` |
| 3 | Trailing `\n` → `.strip()` срабатывает | OK |

### `test_secret_smoke.py` — 1 кейс

```python
def test_assemble_secret_returns_bytes_of_correct_length():
    secret = _assemble_secret()
    assert isinstance(secret, bytes)
    assert len(secret) == 32
```

**Значение секрета НЕ assert-ируется** — это нарушило бы смысл обфускации и зафиксировало секрет в git-истории тестов.

### `conftest.py` — фикстуры (общие для всех трёх файлов)

- `test_secret: bytes` — фиксированный 32-байтный тестовый секрет (литерал в тестах, НЕ `_assemble_secret()`)
- `make_key(licensee: str, iat: date, exp: date | None, secret: bytes) -> str` — helper генерирует валидный ключ для конкретного тест-кейса (используется в `test_verify_license.py` для всех VALID/EXPIRED/INVALID-вариантов)

### Что НЕ тестируется автоматически

| Цель | Причина |
|---|---|
| `_codec.py` отдельным файлом | exercised через `verify_license` (кейсы 7, 9, 10) |
| `_hmac.py` отдельным файлом | exercised через `verify_license` (кейсы 1, 7, 8) |
| `_dispatch_decoder` отдельно | exercised через `verify_license` (кейс 10) |
| `app.py:main` | только ручной smoke (ниже) |
| `tools/gen_license.py` | только ручной smoke (запустить, проверить вывод) |
| Значение секрета в `_assemble_secret` | сознательно — нарушит обфускацию |
| `hmac.compare_digest` использован (а не `==`) | через ручной code review (blocker-правило §5), не через тест |

### Ratio guardrail

Проектное правило: `LOC(tests) / LOC(code) <= 1.5`. Превышение — флаг over-test, требуется свернуть параметризацией или удалить кейсы. По плану ожидается ~150 LOC тестов на ~400 LOC кода (ratio ~0.4) — норма.

### Ручной smoke (не автоматизируется)

1. Запуск без `license.key` → stderr + exit 1
2. Запуск с битым `license.key` → stderr + exit 1
3. Запуск с EXPIRED ключом → stderr + exit 1 с датой
4. Запуск с VALID ключом → программа стартует нормально
5. `python -m tools.gen_license issue --duration week --licensee "Test"` → печатает валидный ключ; ручная подстановка в `license.key` → программа стартует

---

## 12. Out of Scope

Следующее явно **не реализуется** в данном эпике (YAGNI):

- KMS, HSM, внешние сервисы ключей
- Ротация ключей и механизм отзыва (revocation)
- Привязка к железу: Machine ID, MAC-адрес, серийный номер диска
- Защита от тонкого отката системных часов (сверх `iat`-floor)
- Packer, anti-RE, VM-obfuscation бинаря
- Интерактивный prompt или grace-period при отсутствии `license.key`
- Несколько одновременных лицензий
- Feature flags / entitlements внутри ключа (поле `lic` — только идентификатор, не набор флагов)
- Онлайн-валидация подписи
- GUI-визард для активации

---

## 13. Open Questions

None at this time.

---

## 14. References

- **bd epic:** `gektar_monitor-5yvb`
- **Проект:** `fis-monitor` (Python 3.12+, src-layout `src/fis_monitor/`)
- **Stdlib docs:** [`hmac`](https://docs.python.org/3/library/hmac.html), [`hashlib`](https://docs.python.org/3/library/hashlib.html), [`base64`](https://docs.python.org/3/library/base64.html)
- **PEP 634** — Structural Pattern Matching (используется в `app.py:main`)
- **Vault:** `docs/` (Obsidian vault проекта)
