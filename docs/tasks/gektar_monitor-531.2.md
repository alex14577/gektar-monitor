# Task gektar_monitor-531.2 — Protocol интерфейсы домена (post-review fixes)

## Что сделано

Post-review fix session for `domain/interfaces.py`. Все 7 пунктов из code review закрыты:

- **C1** — `import ast` удалён из шапки `interfaces.py` (стал мёртвым после удаления функций).
- **C2** — дубликаты `EventSubscription`/`ConfigSubscription` удалены из `models.py`. Единственная каноническая копия — в `interfaces.py` (без `@runtime_checkable`). `domain/__init__.py` переключён на импорт из `interfaces`. В `test_models_ext.py` isinstance-проверки заменены на структурные аннотации.
- **M1** — добавлены `description: ClassVar[str]` и `recipient_placeholder: ClassVar[str]` в `Notifier` Protocol (канон: `docs/notifications.md` §ClassVar). `ADR-001` обновлён — перечислены все 6 обязательных ClassVar.
- **M2** — исправлен латентный `AttributeError`: `datetime.UTC` (атрибут класса, не модуля) заменён на `UTC` из `from datetime import UTC`. Три места в `test_interfaces.py`.
- **M3** — добавлен тест `test_notifications_repository_mark_attempt_returns_none_when_terminal` — документирует None-path как ожидаемый контракт (ADR-019 R4-C4).
- **M4** — удалены мёртвые функции `_assert_no_forbidden_imports` и `_check_name` (оба `pragma: no cover`, функциональность покрыта тестом `test_no_forbidden_imports` инлайн).
- **n2** — `ConnectionProvider` уже присутствовал в `__all__` — правок не потребовалось.
- **n3** — добавлены docstring к `mark_visited` и `last_visit`: явно указано что это global (не per-lot) timestamp последнего посещения дашборда.

## Результат

- 29 тестов в `test_interfaces.py` (было 28, добавлен 1 для M3).
- 238 passed, 2 skipped в полном suite.
- `ruff check` чистый (2 pre-existing RUF001 в `conftest.py` — не из этой таски).

## Связи

- [[decisions/ADR-001-notifier-protocol-not-abc]] — обновлён: все 6 ClassVar перечислены.
- [[decisions/ADR-019-notification-state-machine]] — контракт `mark_attempt` None-path задокументирован в тесте.
- [[notifications]] — canon ClassVar для Notifier Protocol.
- [[architecture/03-protocols]] — Protocol слои и subscription handles.
