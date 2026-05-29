---
name: licensing-crypto-hmac
description: HMAC-SHA256 sign/verify, обязательность compare_digest, парсинг дат, nbf-floor
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

## Парсинг дат (v2)

`nbf` и `exp` в payload — строки `YYYY-MM-DD`. Сравнение с `now: datetime` (timezone-aware UTC) выполняется **только по дате**:

```python
from datetime import date

nbf_date = date.fromisoformat(payload["nbf"])
exp_date = date.fromisoformat(payload["exp"])

today = now.date()   # now — datetime(UTC), берём только дату
```

Оба поля **обязательны** в v2 — `date.fromisoformat` вызывается без `get()`.

## Anti-rollback floor (`nbf`)

```python
if today < nbf_date:
    return LicenseResult(INVALID, ...)
```

Если текущая дата раньше даты начала действия — ключ недействителен. Блокирует грубый откат системных часов (дни/годы назад). `nbf` также позволяет выпускать ключи с будущей датой активации — оператор задаёт явный диапазон `[nbf, exp]`.

Тонкий откат (часы/минуты) не блокируется — система явно доверяет системным часам (ограничение зафиксировано в [[licensing/out-of-scope]]).

> **v1-only (удалён в v2):** в v1 anti-rollback floor реализовывался через `iat` (issued-at). В v2 `iat` убран, floor перенесён в `nbf`.

## Граничные случаи (зафиксированы для v2)

| Условие | Результат |
|---|---|
| `today == exp_date` | VALID — последний день включён |
| `today == nbf_date` | VALID — первый день включён |
| `today < nbf_date` | INVALID (anti-rollback) |
| `today > exp_date` | EXPIRED |

## Статусы верификации

```
VALID    — подпись корректна, today ∈ [nbf, exp]
EXPIRED  — подпись корректна, today > exp_date
INVALID  — битая подпись, malformed ключ, today < nbf, неизвестная версия
```

Все ошибки парсинга (malformed base64, malformed JSON, неизвестный префикс) → INVALID. Исключения `verify_license` **не выбрасывает** — любая ошибка оборачивается в `LicenseResult(INVALID, ...)`.

## См. также

- [[licensing/key-format|Формат ключа]] — что подписывается
- [[licensing/module-api#_hmac.py|API _hmac.py]] — сигнатуры sign/verify_signature
- [[licensing/secret-obfuscation|Секрет]] — откуда берётся `key=secret`
- [[decisions/ADR-058-license-payload-v2|ADR-058]] — payload v2, nbf-floor
- [[licensing/index|MOC]]
