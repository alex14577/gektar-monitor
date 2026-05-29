---
name: licensing-key-format
description: Строковый формат лицензионного ключа v2, поля payload, base64url правила
type: reference
---

# Формат лицензионного ключа

## Строка ключа (v2)

```
v2.<base64url_payload>.<base64url_sig>
```

- Одна строка, UTF-8, три токена через `.`
- Первый токен — литерал `v2` (идентификатор версии формата)
- `base64url` — алфавит `A-Za-z0-9-_`, **без padding** (символы `=` отсутствуют)

### Восстановление padding при декоде

```python
s += '=' * (-len(s) % 4)
```

Это стандартный приём для base64url без padding.

## Поля payload (v2)

| Поле | Тип | Обязательное | Описание |
|---|---|---|---|
| `v` | `int` | да | Версия формата (для v2 = `2`) |
| `nbf` | `string` | да | Not-before UTC `YYYY-MM-DD`; anti-rollback floor — ключ не действует раньше этой даты |
| `exp` | `string` | да | Expiry UTC `YYYY-MM-DD`; ключ не действует после этой даты |
| `lic` | `string` | да | Тип лицензии — хардкод `"interactive"` в v2 |

Все поля **обязательны**. Отсутствие любого из них → INVALID при верификации.

### Почему `date`, не `datetime`

Сравнение дат выполняется **только по дате** (без часов/минут) — это убирает ложную precision и сложность с timezone в payload. Подробнее: [[licensing/crypto-hmac#Парсинг дат]].

## Канонический JSON

```python
json.dumps(payload_dict, sort_keys=True, separators=(',', ':'))
```

`sort_keys=True` гарантирует стабильный порядок ключей — подписывается именно эта строка. Пробелы убраны (`separators=(',', ':')`) — минимальный размер.

### Пример payload (до base64url)

```json
{"exp":"2026-12-31","lic":"interactive","nbf":"2026-05-29","v":2}
```

### Пример полного ключа (структурный)

```
v2.eyJleHAiOiIyMDI2LTEyLTMxIiwibGljIjoiaW50ZXJhY3RpdmUiLCJuYmYiOiIyMDI2LTA1LTI5IiwidiI6Mn0.<base64url_sig>
```

## Версионирование формата

Первый токен (`v2`) позволяет диспатчеру `verify_license` выбрать нужный декодер без попытки разобрать payload. Неизвестный префикс (включая `v1`) → `INVALID` немедленно. Это делает добавление v3 возможным без изменения v2-пути.

## Migration v1 → v2

**v1 удалён полностью** в рамках [[decisions/ADR-058-license-payload-v2|ADR-058]]. Ни один v1 ключ не будет принят верификатором — `verify_license` возвращает `INVALID` при любом префиксе, отличном от `v2.`.

Отличия v2 от v1:

| Аспект | v1 | v2 |
|---|---|---|
| Версионный маркер | `v1.` | `v2.` |
| Anti-rollback field | `iat` (issued-at) | `nbf` (not-before) |
| Expiry | опционально | обязательно |
| Licensee | произвольная строка | хардкод `"interactive"` |
| Perpetual keys | поддерживались | не поддерживаются |

## См. также

- [[licensing/crypto-hmac|HMAC-SHA256]] — как payload подписывается
- [[licensing/module-api#_codec.py|Кодек API]] — `encode_payload` / `decode_payload`
- [[decisions/ADR-058-license-payload-v2|ADR-058]] — решение о переходе на v2
- [[licensing/index|MOC]]
