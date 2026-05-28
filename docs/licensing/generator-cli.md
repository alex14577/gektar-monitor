---
name: licensing-generator-cli
description: CLI-генератор ключей — init-secret, issue, dev-only гарантия
type: reference
---

# CLI-генератор лицензионных ключей

## Запуск

```bash
python -m tools.gen_license <command> [options]
```

CLI построен на `argparse` (stdlib). Click не используется — no external deps.

## Команда `init-secret`

```bash
python -m tools.gen_license init-secret
```

**Поведение:**

1. Генерирует `secrets.token_bytes(32)` — случайный 32-байтный секрет
2. Разбивает XOR-ом: `_P1 = random_bytes(32)`, `_P2 = secret XOR _P1`
3. Печатает в stdout готовые Python-литералы для вставки в `_secret.py`:

```
_P1 = b'\x3f\xa1\x...'
_P2 = b'\x51\xd4\x...'
```

4. **Не перезаписывает `_secret.py` автоматически** — только печатает. Ручная вставка — намеренно, устраняет риск случайной ротации.

**Когда использовать:** один раз при первоначальной настройке проекта.

**Предупреждение:** повторный `init-secret` сгенерирует новый секрет и сломает все ранее выпущенные ключи — проверка подписи с новым секретом вернёт INVALID.

## Команда `issue`

```bash
python -m tools.gen_license issue \
    (--duration day|week|month|forever | --expires YYYY-MM-DD) \
    --licensee NAME \
    [--out FILE]
```

### Флаги

| Флаг | Описание |
|---|---|
| `--duration day\|week\|month\|forever` | Вычислить `exp` от сегодняшней UTC-даты |
| `--expires YYYY-MM-DD` | Задать `exp` явно |
| `--licensee NAME` | Обязательный; строка идентификатора получателя |
| `--out FILE` | Записать ключ в файл; по умолчанию — stdout |

`--duration` и `--expires` взаимоисключающие, ровно один обязателен:

```python
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--duration", ...)
group.add_argument("--expires", ...)
```

`--duration forever` → поле `exp` отсутствует в payload (бессрочный ключ).

### Пример

```bash
python -m tools.gen_license issue \
    --duration month \
    --licensee "Acme Corp" \
    --out license.key
```

## Единый источник секрета

`gen_license.py` импортирует `_assemble_secret` из `fis_monitor.licensing._secret` — того же модуля, что использует `app.py`. Генератор и верификатор гарантированно используют одинаковый секрет. Рассинхрон ([[licensing/secret-obfuscation|описание]]) технически невозможен.

## Dev-only гарантия

`tools/gen_license.py` **никогда не упаковывается в дистрибутив**. PyInstaller бундлит только граф импортов от `fis_monitor.app:main`; `tools/` в этом графе не участвует. Импорт приватного `_assemble_secret` из `tools/` — допустимая dev-зависимость, не нарушение публичного API.

Запускать только из репозитория разработчика, не из распакованного релиза.

## См. также

- [[licensing/secret-obfuscation|XOR-обфускация]] — что делает `init-secret` под капотом
- [[licensing/license-key-file|Файл license.key]] — куда класть выпущенный ключ
- [[licensing/key-format|Формат ключа]] — структура генерируемой строки
- [[licensing/index|MOC]]
