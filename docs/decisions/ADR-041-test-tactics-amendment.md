# ADR-041 — Test tactics amendment: wiring layer, log collapse, layer location, pyramid baseline, fake canon

**Status**: Accepted
**Date**: 2026-05-17
**Deciders**: Software Architect
**Tags**: testing, architecture, fakes, pyramid, composition, logging

---

## Context

По мере роста тест-сьюта обнаружились четыре серые зоны, не покрытые `docs/architecture/09-test-strategy.md`:

1. `test_build_container` и аналогичные wiring-тесты ошибочно размещались в `tests/unit/services/`, что нарушало инвариант Layer 2 «без реальных зависимостей». Мокирование контейнера — anti-pattern: теряется именно то, что wiring-тест обязан проверить.

2. Logging satellite-тесты дублировали fixtures между файлами и раздувались выше разумного порога LOC без параметризации.

3. Тесты сервисного слоя делали `import sqlite3` напрямую, что создавало скрытую инфраструктурную зависимость в слое, который по контракту не должен знать о persistence-движке.

4. Соотношение файлов по слоям и порог test:code ratio нигде не зафиксированы — writer-агенты принимали решения произвольно.

5. Protocol-fakes дрейфовали от подписи Protocol (разные callsite, разные сигнатуры, нет единого canonical fake).

---

## Decision

Зафиксировать пять тактических правил как поправки к `docs/architecture/09-test-strategy.md` (append пяти новых секций после существующего раздела «Что всегда мокируем»):

1. **Wiring tests are Layer 5 (smoke)** — wiring-тесты принадлежат Layer 5 (smoke), не Layer 2.
2. **Logging parametrize-collapse rule** — лог-тесты ≤120 LOC; общие fixtures → shared conftest; однотипные assert → parametrize.
3. **Layer location rule** — `import sqlite3` запрещён в `tests/unit/services/`; persistence-движок виден только в `tests/integration/` и `tests/unit/infra/`.
4. **Pyramid baseline (non-binding)** — ориентир по доле файлов по слоям; LOC ratio test:code ≤2:1.
5. **Fake signature canon** — один canonical fake на Protocol в `tests/fakes/<protocol_name>.py`, проверяется mypy --strict.

---

## Alternatives Considered

| Вариант | Причина отклонения |
|---|---|
| Оставить wiring-тесты в Layer 2 с мокированием контейнера | Мокирование контейнера уничтожает ценность теста — проверяется mock, а не реальный граф. Нарушает инвариант «unit = без реальных зависимостей» |
| LOC-limit для log-тестов не фиксировать, полагаться на code-review | Без явного числа reviewer не имеет объективного критерия для blocker; порог нужен |
| Разрешить `import sqlite3` в `tests/unit/services/` через «infra helper» | Любой import persistence-движка создаёт coupling к конкретному адаптеру; domain/service-тест через InMemory-fakes именно для этого |
| Pyramid с gate (CI fail при нарушении ratio) | Overhead CI + false-positives при переходных состояниях; ориентир non-binding достаточен |
| Несколько fakes на Protocol в разных test-файлах | Приводит к drift: callsite A тестирует один контракт, callsite B — другой; canonical fake — SSOT |

---

## Consequences

- **Ломает** существующее размещение `test_build_container` — файл перемещается из `tests/unit/` в `tests/smoke/` или объединяется с Layer 5.
- **Требует рефакторинга** logging-тестов при следующем касании (not immediate blocker, но tech-debt, фиксируется в bd).
- **Запрещает** `import sqlite3` в `tests/unit/services/` — статически проверяется через import-linter rule (новое правило добавить в `.importlinter`).
- **Fakes** переезжают в `tests/fakes/` — изменение структуры директорий; conftest-импорты обновляются.
- **Не изменяет** производственный код — только тест-инфраструктуру и docs.
- **Pre-condition для downstream-тасок:** import-linter контракт `no-sqlite-in-unit-services` должен быть добавлен в `.importlinter` ДО merge таски 6wry (relocate sqlite3-importing tests). Без него правило §Layer location rule невидимо CI и легко регрессирует.
- **Pre-condition для canonical-fake адопции:** scaffold `tests/fakes/` и `tests/smoke/` создаётся таской akuj (ADR-041 не создаёт каталоги сам — только декларирует правило). После akuj inline-fakes из остальных файлов (`test_monitor_cycle_with_filter.py`, `test_lot_query.py`, `test_full_scan_service.py`, `test_monitor_cycle_trigger.py`, `test_backfill_sse.py`) подлежат миграции отдельными bd-тасками (не блокирующими ADR-041).

---

## References

- [[architecture/09-test-strategy]] — расширяемый документ тест-стратегии
- [[decisions/ADR-004-composition-root-container-infra-services|ADR-004]] — Container и composition root
- [[decisions/ADR-006-import-linter-ci|ADR-006]] — import-linter в CI
- [[decisions/ADR-001-notifier-protocol-not-abc|ADR-001]] — Protocol как контракт
- [[glossary#wiring-test]], [[glossary#canonical-fake]], [[glossary#pyramid-baseline]]
