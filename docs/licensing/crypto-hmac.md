---
name: licensing-crypto-hmac
description: HMAC-SHA256 sign/verify, обязательность compare_digest, парсинг дат, iat-floor
type: reference
---

# HMAC-SHA256 — криптографическая основа верификации

## Подпись

```python
sig = hmac.new(
    key=secret,
    msg=payload_bytes,
    digestmod=hashlib.sha256,
).digest()
```

`payload_bytes` — UTF-8 байты канонического JSON ([[licensing/key-format#Канонический JSON|формат]]).
Именованные аргументы `key=` и `msg=` **обязательны** — защита от случайной перестановки позиционных аргументов.

## Верификация — constant-time

```python
expected = hmac.new(key=secret, msg=payload_bytes, digestmod=hashlib.sha256).digest()
ok = hmac.compare_digest(expected, received_sig_bytes)
```

**Blocker-правило:** использовать `hmac.compare_digest`, **не** `==`. Обычное сравнение `==` уязвимо к timing-атаке (ранний возврат при первом различающемся байте). Нарушение — blocker-дефект при code review.

## Парсинг дат

`iat` и `exp` в payload — строки `YYYY-MM-DD`. Сравнение с `now: datetime` (timezone-aware UTC) выполняется **только по дате**:

```python
from datetime import date

iat_date = date.fromisoformat(payload["iat"])
exp_raw  = payload.get("exp")        # None или отсутствует → бессрочный
exp_date = date.fromisoformat(exp_raw) if exp_raw else None

today = now.date()                   # now — datetime(UTC), берём только дату
```

## Anti-rollback floor (`iat`)

```python
if today < iat_date:
    return LicenseResult(INVALID, ...)
```

Если текущая дата раньше даты выпуска — ключ недействителен. Блокирует грубый откат системных часов (дни/годы назад). Тонкий откат (часы/минуты) не блокируется — система явно доверяет системным часам (ограничение зафиксировано в [[licensing/out-of-scope]]).

## Граничные случаи (зафиксированы)

| Условие | Результат |
|---|---|
| `today == exp_date` | VALID — последний день включён |
| `today == iat_date` | VALID — день выпуска включён |
| `exp` отсутствует или `null` | Никогда EXPIRED (бессрочный) |
| `today < iat_date` | INVALID |
| `today > exp_date` | EXPIRED |

## Статусы верификации

```
VALID    — подпись корректна, today ∈ [iat, exp] (или exp отсутствует)
EXPIRED  — подпись корректна, today > exp_date
INVALID  — битая подпись, malformed ключ, today < iat, неизвестная версия
```

Все ошибки парсинга (malformed base64, malformed JSON, неизвестный префикс) → INVALID. Исключения `verify_license` **не выбрасывает** — любая ошибка оборачивается в `LicenseResult(INVALID, ...)`.

## См. также

- [[licensing/key-format|Формат ключа]] — что подписывается
- [[licensing/module-api#_hmac.py|API _hmac.py]] — сигнатуры sign/verify_signature
- [[licensing/secret-obfuscation|Секрет]] — откуда берётся `key=secret`
- [[licensing/index|MOC]]
