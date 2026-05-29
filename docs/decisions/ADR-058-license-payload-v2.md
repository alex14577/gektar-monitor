---
name: ADR-058-license-payload-v2
description: Payload v2 — обязательный exp, nbf-floor вместо iat-floor, удаление v1
type: decision
---

# ADR-058 — License Payload v2

**Status:** Accepted  
**Date:** 2026-05-29  
**Supersedes (partial):** [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]] — §payload, §iat-floor, §perpetual  
**Related:** [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]]

## Context

ADR-056 закрепил payload v1:

```json
{"v": 1, "iat": "YYYY-MM-DD", "exp": "YYYY-MM-DD", "lic": "Acme Corp"}
```

По мере использования выявились три слабости:

1. **Perpetual-ключи** (без `exp`) — нарушают fail-closed политику: ключ, однажды выпущенный, действует вечно. В контексте `fis-monitor` perpetual — ошибочная семантика.
2. **`iat` как anti-rollback floor** — `iat` это время *выпуска*, не время *начала действия*. Для случаев «ключ активируется с будущей даты» нужно явное поле `nbf` (not-before).
3. **`lic` как свободная строка** — оператор может опечататься в имени получателя; для `interactive`-только продукта `lic` всегда хардкодируется строкой `"interactive"`.

Дополнительно: bd `gektar_monitor-5bf6` требует интерактивный режим CLI (`run_interactive`), который добавляет новые модули `_prompt.py` и `_interactive.py` в граф лицензирования.

## Decision

**Payload v2:**

```json
{"v": 2, "nbf": "YYYY-MM-DD", "exp": "YYYY-MM-DD", "lic": "interactive"}
```

| Поле | Тип | Обязательное | Описание |
|---|---|---|---|
| `v` | `int` | да | Версия = `2` |
| `nbf` | `string` | да | Not-before: нижняя граница действия, ISO date |
| `exp` | `string` | да | Expiry: верхняя граница действия, ISO date |
| `lic` | `string` | да | Фиксировано `"interactive"` — тип лицензии |

**Изменения логики верификации:**

```python
today = now.date()
if today < nbf_date:   # anti-rollback (nbf-floor)
    return INVALID
if today > exp_date:
    return EXPIRED
return VALID            # nbf_date <= today <= exp_date
```

**v1 удалён полностью** — `_verify.py` принимает только `v2.` префикс. Любой другой (включая `v1.`) → INVALID с причиной «unsupported version».

**`lic` хардкод `"interactive"`** — оператор не вводит; `_build_v2_key` фиксирует значение при сборке payload.

**Новые модули:**

- `_prompt.py` — `Prompter` Protocol + `ConsolePrompter` реализация (IO-boundary)
- `_interactive.py` — `run_interactive(...)` application logic (DI, pure-testable)

## Alternatives considered

### Оставить v1, добавить поля nbf/required-exp через флаги

- Минус: backward-compat на уровне payload усложняет верификатор; «вечные» v1 ключи продолжают работать
- Минус: нет ни одного действующего v1 ключа в проде (секрет перевыпущен до релиза)
- Отвергнуто: чистый разрыв без миграционного груза

### Сохранить perpetual-ключи (exp опционален в v2)

- Минус: создаёт неустранимую дыру в fail-closed политике
- Минус: LicenseExpirySupervisor усложняется (ветка «бесконечный срок»)
- Отвергнуто: exp обязателен

### `lic` как свободная строка в v2

- Плюс: большая гибкость для многопользовательских сценариев
- Минус: в текущем продукте ровно один тип лицензии; свобода = источник ошибок оператора
- Отложено: расширяемость через новую версию payload (v3) при необходимости

## Consequences

**Позитив:**
- `_verify.py` проще: один код-путь, нет ветки perpetual/optional-exp
- anti-rollback через `nbf` явен и контролируем (оператор задаёт дату начала)
- `exp` всегда присутствует → LicenseExpirySupervisor не имеет неявных исключений
- Интерактивный режим CLI (`_interactive.py`) полностью тестируем через DI без мокирования FS/stdin

**Негатив / риски:**
- Все v1 ключи инвалидированы → ни одного ключа к старому формату (принято: секрет перевыпущен)
- `lic="interactive"` не различает клиентов → не проблема для single-tenant продукта

## Изменения файлов

- `src/fis_monitor/licensing/_verify.py` — переписан: только v2, nbf-floor, exp обязателен; v1-литерал → INVALID
- `src/fis_monitor/licensing/cli.py` — переписан: `_build_v2_key`, `issue --nbf --exp --out DIR`, интерактивный режим через `run_interactive`
- `src/fis_monitor/licensing/_prompt.py` — новый: `Prompter` Protocol + `ConsolePrompter`
- `src/fis_monitor/licensing/_interactive.py` — новый: `run_interactive(...)` pure DI flow + `_default_save_dir()`
- `tests/licensing/conftest.py` — `make_v2_key` (вместо `make_key` v1)
- `tests/licensing/test_verify_license.py` — переписан под v2 инварианты (10 кейсов + determinism smoke)
- `tests/licensing/test_interactive.py` — новый: 7 инвариантов `run_interactive` + `_default_save_dir` branches
- `tests/licensing/test_cli.py` — новый: smoke issue subcommand
- `tests/unit/services/test_license_expiry.py` — `_make_v2_key`, perpetual-тест заменён на valid-loops-тест

## См. также

- [[decisions/ADR-056-licensing-hmac-stateless-offline|ADR-056]] — HMAC-SHA256, stateless, XOR-обфускация
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]] — CLI как console_script
- [[licensing/architecture|архитектура модулей]]
- [[licensing/crypto-hmac|HMAC детали]]
- [[licensing/key-format|формат ключа]]
