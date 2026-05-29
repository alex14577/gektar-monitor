---
name: licensing-test-strategy
description: Модульное тестирование подсистемы лицензирования через публичный контракт — минимум кейсов, максимум инвариантов
type: reference
---

# Стратегия тестирования — подсистема лицензирования

## Принцип: тесты на МОДУЛЬ, не на функцию

Юнит-тесты пишутся на **модули** через их публичную поверхность. Внутренние функции (`encode_payload`, `decode_payload`, `sign`, `verify_signature`, `_decode_v2`) **не тестируются напрямую** — они exercised транзитивно через тесты публичного API.

**Что это даёт:**

- ~10× меньше тестов при том же покрытии инвариантов
- Свобода рефакторинга внутренностей без переписывания тестов
- Тесты документируют ПОВЕДЕНИЕ, не структуру кода

**На практике:**
VALID-ключ принят `verify_license` → значит кодек + HMAC + парсинг дат работают. Tampered-ключ отвергнут → значит `compare_digest` + подпись работают. Дублировать это в отдельных `test_codec.py` / `test_hmac.py` — over-test.

## Что тестируем автоматически

| Модуль | Публичная функция / контракт | Тестовый файл |
|---|---|---|
| `licensing` (фасад) | `verify_license(key_str, secret, now)` | `tests/licensing/test_verify_license.py` |
| `_interactive.py` | `run_interactive(...)` инварианты + `_default_save_dir` | `tests/licensing/test_interactive.py` |
| `_license_loader` | `load_license_key(anchor)` | `tests/licensing/test_license_loader.py` |
| `_secret` | `_assemble_secret()` | `tests/licensing/test_secret_smoke.py` |
| `cli.py` | `main(["issue", ...])` smoke | `tests/licensing/test_cli.py` |

**Пять файлов — всё автотестовое покрытие.**

## `test_verify_license.py` — параметризованный (10 кейсов + determinism smoke)

| # | Кейс | Ожидание |
|---|---|---|
| 1 | VALID: today ∈ [nbf, exp] | `VALID, expires_at=date` |
| 2 | VALID: today == nbf (нижняя граница) | `VALID` |
| 3 | VALID: today == exp (верхняя граница) | `VALID` |
| 4 | INVALID: today < nbf (anti-rollback) | `INVALID` |
| 5 | EXPIRED: today > exp | `EXPIRED` |
| 6 | INVALID: tampered payload | `INVALID` |
| 7 | INVALID: tampered signature | `INVALID` |
| 8 | INVALID: malformed base64 | `INVALID` |
| 9 | INVALID: неизвестный префикс версии | `INVALID` |
| 10 | INVALID: v1-литерал (`v1.…`) | `INVALID` (unsupported version) |

Плюс determinism + payload structure smoke: два одинаковых вызова → одинаковый результат (нет скрытого `datetime.now()`); payload содержит `v==2`, `nbf`, `exp`, `lic=="interactive"`.

## `test_interactive.py` — 7 инвариантов + anti-fake + _default_save_dir

| # | Кейс |
|---|---|
| anti-fake | `RecordingPrompter` вызывает все методы `Prompter` |
| 1 | happy path: все валидные ответы → writer вызван с правильным path, key=v2, return 0 |
| 2 | `exp < nbf` → error + retry exp |
| 3 | неверный формат nbf → error + retry nbf |
| 4 | несуществующая директория → error + retry dir |
| 5 | файл существует + overwrite=False → retry dir |
| 6 | файл существует + overwrite=True → writer вызван |
| 7 | OSError из key_writer → error, return 1 |
| extra | `_default_save_dir()`: оба бранча (frozen / not frozen) через monkeypatch |

**Layer:** application logic. Все зависимости инжектируются; `ConsolePrompter` (IO-адаптер) — НЕ тестируется.

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

## `test_cli.py` — 2 smoke кейса

1. `issue --nbf --exp --out DIR` → exit 0, `license.key` создан, начинается с `v2.`
2. `issue` с `exp < nbf` → exit 1

## `conftest.py` — общие фикстуры

- `test_secret: bytes` — 32 байта фиксированного тестового секрета (литерал, **не** `_assemble_secret()`)
- `make_v2_key(nbf, exp, secret, lic="interactive") -> str` — helper генерирует валидный v2 ключ

## Что НЕ тестируется автоматически

| Цель | Причина |
|---|---|
| `_codec.py` отдельно | exercised через `verify_license` (кейсы 6, 8, 9) |
| `_hmac.py` отдельно | exercised через `verify_license` (кейсы 1, 6, 7) |
| `_prompt.py::ConsolePrompter` | IO-адаптер (input/print); Layer = infrastructure, не покрывается unit-тестами |
| `app.py:main` | только ручной smoke |
| Значение секрета | нарушит обфускацию |
| `hmac.compare_digest` (а не `==`) | ручной code review (blocker-правило) |

## Ratio guardrail

Проектное правило: `LOC(tests) / LOC(code) ≤ 1.5`. Превышение — флаг over-test.

## Ручной smoke для `app.py:main` и CLI

Сценарии: [[licensing/manual-smoke|manual-smoke.md]].

## Почему `ConsolePrompter` не покрывается unit-тестами

`ConsolePrompter` — IO-адаптер: обёртка над `input()` / `print()`. Тест потребовал бы перехвата stdin/stdout — низкая ценность при высоком maintenance-cost. Поведение already покрыто: `run_interactive` тестируется через `RecordingPrompter` fake (DI seam). Любой регресс `ConsolePrompter` поймает ручной smoke.

## Почему `app.py:main` не покрывается unit-тестами

`main()` — composition root: материализует все зависимости (`_assemble_secret`, `datetime.now`, `sys.exit`). Unit-тест потребовал бы мокирования всего стека, что даёт низкую ценность при высоком maintenance-cost. Поведение already покрыто через `verify_license` и `load_license_key`.

## См. также

- [[licensing/module-api|Публичный API]] — тестируемые сигнатуры
- [[licensing/crypto-hmac|HMAC]] — граничные случаи дат
- [[decisions/ADR-058-license-payload-v2|ADR-058]] — v2 payload, nbf-floor
- [[licensing/index|MOC]]
