# Архитектура `fis-monitor` (MOC)

> Документ-stub. Атомарные ноты — в `docs/architecture/`. Источник правды по решениям — [[decisions-log]].

Принципы — SOLID, DI через конструктор, Protocols для всех внешних швов, high cohesion / low coupling, composition over inheritance, расширение через регистрацию реализации интерфейса (не модификация).

## Оглавление

- [[architecture/00-open-questions-resolved]] — 7 открытых вопросов закрыты + изменения относительно `notifications.md`
- [[architecture/01-container-diagram]] — C4 Level 2
- [[architecture/02-layers-dip]] — слои и направление зависимостей
- [[architecture/03-protocols]] — полный список Protocol-интерфейсов (~15 швов)
- [[architecture/04-composition-root]] — Container/Infra/Services, two-phase shutdown, lifespan
- [[architecture/05-extension-points]] — Open/Closed таблица расширений
- [[architecture/06-notifier-registry]] — Plugin discovery (explicit registry)
- [[architecture/07-concurrency]] — потокобезопасность, SQLite maintenance, SSE fan-out, hot-reload
- [[architecture/08-error-strategy]] — exception vs Result, UpstreamError
- [[architecture/09-test-strategy]] — тесты по слоям
- [[architecture/10-project-structure-diffs]] — итоговое дерево `src/fis_monitor/`
- [[architecture/10-7-diagnostic-zip]] — diagnostic export + redactor + schema-snapshot
- [[architecture/10-8-backup-strategy]] — backup user-state only
- [[architecture/10-9-http-logs]] — fields-whitelist для `requests.jsonl`
- [[architecture/99-open-questions]] — текущий статус open questions

## ADR-блоки

Все архитектурные решения вынесены в атомарные ADR — см. [[decisions-log]] (MOC). Core: ADR-001..022.

## См. также

- [[decisions-log]] — все зафиксированные решения, ADR-001..022
- [[onboarding]] — server-side onboarding FSM ([[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]])
- [[project-structure]] — текущая раскладка
- [[data-model/lot]], [[data-model/notifications]], [[data-model/settings]], [[data-model/sse]], [[data-model/errors]]
- [[notifications]] — плагин-архитектура каналов
- [[product/monitoring-plan]] — поток данных, потоки исполнения
- `db/schema.sql` — схема БД
- [[ops/runbook]] — failure modes
