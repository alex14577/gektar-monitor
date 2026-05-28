---
name: licensing-secret-obfuscation
description: XOR-схема обфускации секрета, honest security tradeoff, DI-инвариант
type: reference
---

# XOR-обфускация секрета

## Мотивация

Хранить секрет строкой-константой — найти его через `strings` на бинаре за секунды.
XOR-сборка из двух частей убирает опознаваемую константу. Атака через дизассемблер всё равно сработает — это **security-through-obscurity**, принятый явно ради потолка сложности 2/10.

Подробнее о трейдоффе: [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]].

## Структура `_secret.py`

```python
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
- `len(_P1) == len(_P2) == 32` — результирующий секрет = 256 бит
- `_P1`, `_P2` — **локальные переменные** внутри функции, не модульные константы
- Функция — чистая, без I/O, без side-effects
- Секрет вычисляется только при вызове, нигде не кэшируется на уровне модуля

## Как инициализировать секрет (одноразово)

CLI-команда `init-secret` (см. [[licensing/generator-cli]]) генерирует случайный 32-байтный секрет, разбивает XOR-ом и печатает готовые Python-литералы в stdout. Файл `_secret.py` **не перезаписывается автоматически** — только вывод для ручной вставки. Это устраняет риск случайной ротации.

**Повторный `init-secret` сломает все ранее выпущенные ключи** — новый секрет, старые подписи невалидны.

## DI-инвариант — секрет как параметр

`verify_license` и `gen_license.py` не вызывают `_assemble_secret()` изнутри. Секрет передаётся параметром:

```python
# В app.py:main — единственное место вызова _assemble_secret() в production-коде
result = verify_license(
    key_str,
    secret=_assemble_secret(),
    now=datetime.now(timezone.utc),
)
```

**Почему это важно:** тесты подставляют произвольный тестовый секрет через параметр, не зная значений `_P1`/`_P2`. Тестируемость без раскрытия реального секрета.

## Единый источник секрета

`gen_license.py` импортирует `_assemble_secret` из того же модуля `fis_monitor.licensing._secret`, что использует `app.py`. Генератор и верификатор гарантированно используют одинаковый секрет — рассинхрон невозможен.

## Граница dev-only

`tools/gen_license.py` — **dev-инструмент, не упаковывается в дистрибутив**. PyInstaller бундлит только граф импортов от `fis_monitor.app:main`; `tools/` в этом графе не участвует. Импорт приватного `_assemble_secret` из `tools/` — допустимая dev-зависимость, не нарушение API.

## Что XOR не защищает

- **Дизассемблер или отладчик** — атакующий с мотивацией извлечёт секрет и сможет генерировать произвольные действительные ключи
- **Нет механизма отзыва** — скомпрометированный секрет означает пересборку бинаря

Это осознанный tradeoff. Альтернативы (KMS, HSM, онлайн-валидация) — в [[licensing/out-of-scope]].

## См. также

- [[licensing/generator-cli|CLI `init-secret`]] — генерация пар `_P1/_P2`
- [[licensing/module-api|Публичный API]] — DI через параметр `secret: bytes`
- [[licensing/index|MOC]]
