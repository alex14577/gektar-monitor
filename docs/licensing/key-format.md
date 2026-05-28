---
name: licensing-key-format
description: Строковый формат лицензионного ключа, поля payload, base64url правила
type: reference
---

# Формат лицензионного ключа

## Строка ключа

```
v1.<base64url_payload>.<base64url_sig>
```

- Одна строка, UTF-8, три токена через `.`
- Первый токен — литерал `v1` (идентификатор версии формата)
- `base64url` — алфавит `A-Za-z0-9-_`, **без padding** (символы `=` отсутствуют)

### Восстановление padding при декоде

```python
s += '=' * (-len(s) % 4)
```

Это стандартный приём для base64url без padding.

## Поля payload

| Поле | Тип | Обязательное | Описание |
|---|---|---|---|
| `v` | `int` | да | Версия формата (для v1 = `1`) |
| `iat` | `string` | да | Дата выпуска UTC `YYYY-MM-DD`; anti-rollback floor |
| `exp` | `string \| null` | нет | Дата истечения UTC `YYYY-MM-DD`; отсутствие или `null` = бессрочный |
| `lic` | `string` | да | Идентификатор получателя (произвольная строка) |

### Почему `date`, не `datetime`

Сравнение дат выполняется **только по дате** (без часов/минут) — это убирает ложную precision и сложность с timezone в payload. Подробнее: [[licensing/crypto-hmac#Парсинг дат]].

## Канонический JSON

```python
json.dumps(payload_dict, sort_keys=True, separators=(',', ':'))
```

`sort_keys=True` гарантирует стабильный порядок ключей — подписывается именно эта строка. Пробелы убраны (`separators=(',', ':')`) — минимальный размер.

### Пример payload (до base64url)

```json
{"exp":"2026-12-31","iat":"2026-05-28","lic":"Acme Corp","v":1}
```

### Пример полного ключа (структурный)

```
v1.eyJleHAiOiIyMDI2LTEyLTMxIiwiaWF0IjoiMjAyNi0wNS0yOCIsImxpYyI6IkFjbWUgQ29ycCIsInYiOjF9.W3NpZ25hdHVyZV9ieXRlc19oZXJlXQ
```

## Версионирование формата

Первый токен (`v1`) позволяет диспатчеру `_dispatch_decoder` выбрать нужный декодер без попытки разобрать payload. Неизвестный префикс → `INVALID` немедленно. Это делает добавление v2 возможным без изменения v1-пути.

## См. также

- [[licensing/crypto-hmac|HMAC-SHA256]] — как payload подписывается
- [[licensing/module-api#_codec.py|Кодек API]] — `encode_payload` / `decode_payload`
- [[licensing/index|MOC]]
