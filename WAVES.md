# Волны параллельного выполнения

Дата старта: 2026-05-15.

## Закрытые как stale/duplicate (до волн)

- **3m3** — `feat(regions): прокидывать Settings.subject_site_ids в backfill/full_scan/monitor_cycle`. Закрыта: поле `Settings.subject_site_ids` удалено в bd-6f6 (ADR-035), таска протухла.
- **add** — `PaginatedListFetcher.iterate: expose current page in progress`. Закрыта как дубликат **r7d**.
- **0qn** — `GET /backfill/status endpoint + feed.html.jinja polling`. Закрыта как дубликат **ij9**.
- **ufk** — `UI progress widget for backfill`. Закрыта как дубликат **c8o**.

## Волна 1 — параллельно, без пересечения файлов

Все 7 задач запущены одновременно через специализированных sub-agents (python-pro / Frontend Developer).

| ID | Тип | Файлы | Что делает |
|----|-----|-------|------------|
| **amo** | bug P2 | `web/routes/lots.py` + тест | Добавить `GET /lots/{lot_id}/redirect` → 302 на каноничный URL torgi.gov.ru. Сейчас 404. Тест: 302+Location для существующего лота, 404 для несуществующего. |
| **7om** | task P2 | `web/templates/partials/_sidebar_filters.html.jinja` | Sidebar Вариант A: заголовок «Что показывать» → «Область мониторинга», убрать префикс «Наблюдаю: », иконка глаза → шестерёнка, физический разделитель (border-top) от блока «Фильтры ленты». |
| **8z2** | task P4 | `infra/config_source.py` | DEBUG-log при дропе невалидного `rf_subjects` id (catalog drift после обновления каталога регионов). |
| **5oq** | task P2 | `infra/parsers/list_parser.py` + тест | ESIA detection: тест на Signal 2 (inline `window.location` в `<head>` script) + новый Signal 3 (`<form action="esia.gosuslugi.ru/...">`). Defense-in-depth. |
| **z9d** | task P2 | `domain/interfaces.py` ← `domain/models.py` | Перенести `EventSubscription` / `ConfigSubscription` Protocol из `models.py` в `interfaces.py` (SRP). Уже было выполнено превентивно — оставить как есть. |
| **12y** | task P2 | `infra/sqlite/repositories/state.py` (новый) + Protocol + wiring | KV-репозиторий поверх таблицы `state(key, value, updated_at)` для `last_critical_event:*`, `session_expired_email_sent` и подобных flag-ов. Protocol + SqliteStateRepository + регистрация в Container/composition. 18 unit-тестов Layer 1. |
| **r7d** | task P3 | `services/paginated_list_fetcher.py` + `services/backfill.py` | `iterate()` принимает опциональный `page_callback(page_num, items_count)`. BackfillService теперь обновляет `_progress.current_page` через callback вместо параллельного счётчика. |

## Волна 2 — план (после ревью/коммита Волны 1)

Группы по файлам без пересечения:

### 2a. Domain / security (последовательно по smtp_credentials.py)
- **ctz** P2 — `domain/smtp_credentials.py`: запретить pickle/multiprocessing/faulthandler (override `__reduce__` / `__getstate__`).
- **ljp** P2 — `domain/smtp_credentials.py`: решить судьбу поля `smtp_from_name` (wizard step 2 inconsistency: либо добавить, либо удалить).

> ⚠️ Конфликт по файлу — выполнять последовательно: сначала ljp (решение поля), потом ctz (security-ограничение поверх итогового класса).

### 2b. Параллельно (нет пересечений)
- **dmu** P2 — `services/enrichment.py`: `EnrichmentService.bind_executor` seam (DI для ThreadPoolExecutor).
- **e1w** P2 — `infra/http/*`: hardening TLS — bundle Russian Trusted CA вместо `verify=False`.
- **dzm** P2 — session-expired email: новый event-type `session.expired` в существующий `notifier_dispatcher`, шаблон, idempotency через `state` table (использует Волну-1 `StateRepository`).
- **d7u** P2 — `tools/fake_torgi/` (новый): staging-сервер для headed Playwright-тестов.
- **0kx** P2 — `.claude/hooks/`: bd-status-on-stop + vault-update-check + commit-without-bd-block.
- **ij9 + c8o** P3 — связка: `GET /backfill/status` endpoint + UI progress widget (общая фича, делать одной таской).

### 2c. После 2b
- **014** P2 — Wave 1 audit minors: rate-limits + input validation cleanup (multi-place, требует свежей кодовой базы).

## Текущий статус Волны 1

Все 7 sub-agents отчитались успехом. Следующий шаг — ревью каждого diff'а, прогон полного `pytest` + `ruff`, коммиты по таскам, `bd close`, vault-update там где нужно (12y → `docs/data-model/`).
