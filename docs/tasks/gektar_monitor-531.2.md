---
bd-id: gektar_monitor-531.2
title: domain/interfaces.py — Protocol seams (~18)
status: closed
closed: 2026-05-13
files:
  - src/fis_monitor/domain/interfaces.py
  - tests/unit/domain/__init__.py
  - tests/unit/domain/test_interfaces.py
---

# domain/interfaces.py — Protocol seams (~18)

## Что сделано

Создан `src/fis_monitor/domain/interfaces.py` — единственный файл всех
Protocol-швов (seams) проекта. Перенесены `EventSubscription[T]` и
`ConfigSubscription` из `models.py` (follow-up z9d). Написаны 28 unit-тестов.

### Перечень всех Protocol-ов

**Layer 0 — системные утилиты:**
- `Clock` (`@runtime_checkable`) — wall-clock + monotonic
- `ConnectionProvider` — per-thread SQLite-коннект (infra-internal seam)
- `Locker` — OS-level single-instance lock
- `ConfigSource` — hot-reload конфига с подпиской
- `EventBus` — sync→async мост для SSE fan-out

**Layer 1 — репозитории:**
- `LotRepository` — upsert/get/list/mark_* лотов (ADR-016)
- `UserStateRepository` — starred/submitted/note/visit
- `NotificationsRepository` — state machine отправок pending→sent|permanent_fail (ADR-019)
- `SettingsRepository` — KV таблица `state`, onboarding FSM
- `SmtpCredentialsRepository` — singleton SMTP-креды в state.db (ADR-020)
- `CyclesRepository` — открытие/закрытие циклов мониторинга

**Layer 2 — адаптеры внешних систем:**
- `HttpClient` — sync GET с HttpResponse
- `ListParser` — парсинг HTML-списка лотов
- `DetailParser` — парсинг карточки лота
- `LoginSession` — headed-login через Playwright
- `SmtpHostPolicy` — DNS-resolve + policy-check (ADR-015, ADR-021)
- `AutostartManager` — Windows Task Scheduler / XDG Autostart
- `MigrationRunner` — schema migration по user_version

**Layer 3 — плагин уведомлений:**
- `Notifier` (`@runtime_checkable`) — канал уведомлений с `ClassVar channel_id` (ADR-001)

**Subscription handles (follow-up z9d):**
- `EventSubscription[T]` — context-manager handle подписки на EventBus
- `ConfigSubscription` — context-manager handle подписки на reload конфига

Итого: 21 объект в `__all__` (18 именованных seams + 2 subscription handles + ConnectionProvider).

## Почему так

**MigrationRunner включён.** Несмотря на то что brainstorm session #2 склонялся к пропуску,
acceptance criteria bd-таски явно перечисляет его. Минимальный Protocol (`run(target_version: int) -> None`)
дёшев, упрощает тестирование composition root, не нарушает ISP.

**ConnectionProvider включён** как infra-internal seam (не domain-concept), но его Protocol
нужен чтобы репозитории можно было тестировать с fake-провайдером. Архитектура §3 явно
оговаривает его infra-internal статус.

**`@runtime_checkable`** только на `Clock` (duck-typing probe в тестах) и `Notifier`
(registry `isinstance`-guard при регистрации плагинов). Остальные — structural-only,
декоратор на них только добавил бы overhead без пользы.

**EventSubscription использует Python 3.12 type parameter syntax** (`class EventSubscription[T]`)
вместо `Generic[T]` (UP046 ruff rule). Это PEP 695, Python 3.12+ — соответствует стеку проекта.

Все решения согласованы с: [[architecture]] §3, §6; [[decisions-log#ADR-001]];
[[decisions-log#ADR-006]]; [[decisions-log#ADR-016]]; [[decisions-log#ADR-019]].

## Связи

- Закрывает: `bd #gektar_monitor-531.2`
- Follow-up z9d: `EventSubscription`/`ConfigSubscription` перенесены из `models.py`
  (TODO-комментарий в models.py остался — удалить отдельным коммитом)
- Связано: [[architecture]], [[decisions-log#ADR-001]], [[decisions-log#ADR-019]],
  [[decisions-log#ADR-020]], [[notifications]], [[glossary#Protocol]]
- Новые термины: [[glossary#LotRepository]], [[glossary#NotificationsRepository]],
  [[glossary#EventSubscription]], [[glossary#ConnectionProvider]]

## Follow-up

- Удалить `EventSubscription`/`ConfigSubscription` из `models.py` (сейчас дубль с TODO)
- Настроить import-linter (ADR-006) в CI чтобы автоматически ловить нарушение
  `domain → infra/services/web`
- Реализовать конкретные infra-адаптеры под каждый Protocol
