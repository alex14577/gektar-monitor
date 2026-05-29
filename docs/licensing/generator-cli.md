---
name: licensing-generator-cli
description: CLI-генератор ключей — init-secret, issue, интерактивный режим
type: reference
---

# CLI-генератор лицензионных ключей

## Запуск

```bash
gektar-gen-license <command> [options]
# или без аргументов для интерактивного режима:
gektar-gen-license
```

CLI построен на `argparse` (stdlib). Click не используется — no external deps.

## Интерактивный режим

При запуске **без аргументов** (двойной клик или `gektar-gen-license` в терминале без команды) программа входит в guided-режим и задаёт три вопроса:

```
Дата начала действия (nbf, YYYY-MM-DD): 2026-06-01
Дата окончания действия (exp, YYYY-MM-DD): 2026-12-31
Директория для сохранения [Enter = /home/user]: 
Ключ сохранён: /home/user/license.key
Нажмите Enter для выхода…
```

- Неверный формат даты → сообщение об ошибке, вопрос задаётся повторно
- `exp < nbf` → сообщение об ошибке, вопрос `exp` задаётся повторно
- Несуществующая директория → сообщение об ошибке, вопрос директории повторяется
- Если `license.key` уже существует → уточнение «Перезаписать? [y/N]:»
- Файл всегда называется `license.key` (фиксировано)
- Пустой ответ на вопрос директории → использует директорию рядом с исполняемым файлом (frozen) или `cwd()` (dev)
- Ctrl+C → «Отменено.» без паузы

## Команда `init-secret`

```bash
gektar-gen-license init-secret
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
gektar-gen-license issue \
    --nbf YYYY-MM-DD \
    --exp YYYY-MM-DD \
    --out DIR
```

### Флаги

| Флаг | Описание |
|---|---|
| `--nbf YYYY-MM-DD` | Not-before дата (начало действия ключа) — **обязательный** |
| `--exp YYYY-MM-DD` | Expiry дата (конец действия, включительно) — **обязательный** |
| `--out DIR` | Директория, куда записать `license.key` — **обязательный** |

Файл всегда записывается как `<DIR>/license.key`.

Гарды:
- `exp < nbf` → exit 1
- `--out` не существует или не является директорией → exit 1

### Пример

```bash
gektar-gen-license issue \
    --nbf 2026-06-01 \
    --exp 2026-12-31 \
    --out /tmp/
```

Записывает `/tmp/license.key` с ключом формата `v2.<payload>.<sig>`.

## Единый источник секрета

`licensing/cli.py` импортирует `_assemble_secret` из `fis_monitor.licensing._secret` — того же модуля, что использует `app.py`. Генератор и верификатор гарантированно используют одинаковый секрет.

## Распространение и dev-only гарантия

CLI поставляется как console script `gektar-gen-license`, объявленный в `[project.scripts]` (`pyproject.toml`). Доступен после `pip install -e .` или установки wheel-а.

**End-user дистрибутив (PyInstaller)** бундлит только импорт-граф `fis_monitor.app:main`. `fis_monitor.licensing.cli` в этом графе не участвует — в распакованном `.exe` CLI всё равно недоступен. Подробности — [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]].

## См. также

- [[licensing/secret-obfuscation|XOR-обфускация]] — что делает `init-secret` под капотом
- [[licensing/license-key-file|Файл license.key]] — куда класть выпущенный ключ
- [[licensing/key-format|Формат ключа]] — структура генерируемой строки v2
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]]
- [[decisions/ADR-058-license-payload-v2|ADR-058]]
- [[licensing/index|MOC]]
