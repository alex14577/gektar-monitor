---
name: licensing-test-strategy
description: Модульное тестирование подсистемы лицензирования через публичный контракт — минимум кейсов, максимум инвариантов
type: reference
---

# Стратегия тестирования — подсистема лицензирования

## Принцип: тесты на МОДУЛЬ, не на функцию

Юнит-тесты пишутся на **модули** через их публичную поверхность. Внутренние функции (`encode_payload`, `decode_payload`, `sign`, `verify_signature`, `_dispatch_decoder`) **не тестируются напрямую** — они exercised транзитивно через тесты публичного API.

**Что это даёт:**

- ~10× меньше тестов при том же покрытии инвариантов
- Свобода рефакторинга внутренностей без переписывания тестов
- Тесты документируют ПОВЕДЕНИЕ, не структуру кода

**На практике:**
VALID-ключ принят `verify_license` → значит кодек + HMAC + парсинг дат работают. Tampered-ключ отвергнут → значит `compare_digest` + подпись работают. Дублировать это в отдельных `test_codec.py` / `test_hmac.py` — over-test.

## Что тестируем автоматически

| Модуль | Публичная функция | Тестовый файл |
|---|---|---|
| `licensing` (фасад) | `verify_license(key_str, secret, now)` | `tests/licensing/test_verify_license.py` |
| `_license_loader` | `load_license_key(anchor)` | `tests/licensing/test_license_loader.py` |
| `_secret` | `_assemble_secret()` | `tests/licensing/test_secret_smoke.py` |

**Три файла — всё автотестовое покрытие.** Никаких отдельных `test_codec.py` / `test_hmac.py` / `test_verify.py`.

## `test_verify_license.py` — параметризованный (10 кейсов)

| # | Кейс | Ожидание |
|---|---|---|
| 1 | VALID: today между iat и exp | `VALID, expires_at=date` |
| 2 | VALID: бессрочный (нет `exp`) | `VALID, expires_at=None` |
| 3 | VALID: today == exp_date | `VALID` |
| 4 | VALID: today == iat_date | `VALID` |
| 5 | EXPIRED: today > exp_date | `EXPIRED` |
| 6 | INVALID: today < iat_date | `INVALID` |
| 7 | INVALID: tampered payload | `INVALID` |
| 8 | INVALID: tampered signature | `INVALID` |
| 9 | INVALID: malformed base64 | `INVALID` |
| 10 | INVALID: неизвестный префикс версии | `INVALID` |

Плюс smoke на чистоту: два одинаковых вызова → одинаковый результат (DI выдержано, нет скрытого `datetime.now()`).

## `test_license_loader.py` — 3 кейса

| # | Кейс | Ожидание |
|---|---|---|
| 1 | Валидный файл → stripped строка | OK |
| 2 | Файл отсутствует | `FileNotFoundError` |
| 3 | Trailing `\n` → `.strip()` срабатывает | OK |

## `test_secret_smoke.py` — 1 кейс

```python
def test_assemble_secret_returns_bytes_of_correct_length():
    secret = _assemble_secret()
    assert isinstance(secret, bytes)
    assert len(secret) == 32
```

**Значение секрета НЕ assert-ируется** — это нарушило бы смысл обфускации и зафиксировало секрет в git-истории.

## `conftest.py` — общие фикстуры

- `test_secret: bytes` — 32 байта фиксированного тестового секрета (литерал, **не** `_assemble_secret()`)
- `make_key(licensee, iat, exp, secret) -> str` — helper генерирует валидный ключ для тест-кейса

## Что НЕ тестируется автоматически

| Цель | Причина |
|---|---|
| `_codec.py` отдельно | exercised через `verify_license` (кейсы 7, 9, 10) |
| `_hmac.py` отдельно | exercised через `verify_license` (кейсы 1, 7, 8) |
| `_dispatch_decoder` отдельно | exercised через `verify_license` (кейс 10) |
| `app.py:main` | только ручной smoke |
| `tools/gen_license.py` | только ручной smoke |
| Значение секрета | нарушит обфускацию |
| `hmac.compare_digest` (а не `==`) | ручной code review (blocker-правило) |

## Ratio guardrail

Проектное правило: `LOC(tests) / LOC(code) ≤ 1.5`. Превышение — флаг over-test. По плану: ~150 LOC тестов / ~400 LOC кода (ratio ~0.4) — норма.

## Ручной smoke для `app.py:main` и CLI

1. Запуск без `license.key` → stderr + exit 1
2. Запуск с битым `license.key` → stderr + exit 1
3. Запуск с EXPIRED ключом → stderr + exit 1 с датой
4. Запуск с VALID ключом → программа стартует
5. `python -m tools.gen_license issue --duration week --licensee "Test"` → валидный ключ → подстановка в `license.key` → программа стартует

## Почему `app.py:main` не покрывается unit-тестами

`main()` — composition root: материализует все зависимости (`_assemble_secret`, `datetime.now`, `sys.exit`). Unit-тест потребовал бы мокирования всего стека, что даёт низкую ценность при высоком maintenance-cost. Поведение already покрыто через `verify_license` и `load_license_key`.

## См. также

- [[licensing/module-api|Публичный API]] — тестируемые сигнатуры
- [[licensing/crypto-hmac|HMAC]] — граничные случаи дат
- [[licensing/index|MOC]]
