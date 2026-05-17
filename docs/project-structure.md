---
title: Структура проекта (src/fis_monitor)
status: canon
---

# Структура проекта

Фактическая раскладка `src/fis_monitor/` с описанием назначения каждого пакета.
Архитектурные принципы: [[architecture/02-layers-dip]] (слои + DIP), [[architecture/04-composition-root]] (сборка зависимостей).

## Корень пакета (`src/fis_monitor/`)

| Файл | Назначение |
|---|---|
| `app.py` | FastAPI application factory + lifespan. Монтирует роуты, запускает supervised threads (monitor cycle, full scan, enrichment, config watchdog). Трёхфазный shutdown per ADR-014. |
| `composition.py` | Composition root: `build_container()` — топологическая сборка всех зависимостей без DI-фреймворка. Создаёт `TorgiUrlBuilder` из `config_source.current().target.base_url`. Слои 0–4. Подробнее: [[architecture/04-composition-root]]. |
| `container.py` | Frozen dataclasses `Infra` (слои 0–2), `Services` (слои 3–4), мutable `Container`. Чёткий шов: сервисы зависят только от Protocol-интерфейсов `Infra`. |

## `domain/`

Чистый доменный слой. Никаких зависимостей на инфраструктуру. См. [[architecture/02-layers-dip]].

| Файл | Назначение |
|---|---|
| `models.py` | Pydantic-модели домена: `Lot`, `LotUserState`, `CycleResult`, `OnboardingState`, `Settings`, `TargetConfig` (base_url / request_timeout_seconds / user_agent, ADR-024), `EmailConfig` и т.д. Один источник правды по defaults и валидации `config.json`. |
| `interfaces.py` | Протоколы (Protocols) для всех внешних зависимостей: `LotRepository`, `HttpClient`, `ListParser`, `DetailParser`, `Clock`, `EventBus`, `Locker`, `NotificationsRepository` и др. Инверсия зависимостей (DIP). |
| `diff.py` | Алгоритм сравнения снапшотов лотов (new/removed/changed). |
| `errors.py` | Доменные исключения. |
| `regions.py` | Константы макро-регионов (ДФО=1, Арктика=2) и отображение субъектов. |

## `infra/`

Инфраструктурный слой. Реализует Protocols из `domain/interfaces.py`. [[architecture/02-layers-dip]] §DIP.

### `infra/http/`

| Файл | Назначение |
|---|---|
| `client.py` | `RequestsHttpClient` — реализация `HttpClient` Protocol поверх `requests`. `verify=False` для upstream с self-signed cert (ADR-024 §SSL). |
| `url_builder.py` | `TorgiUrlBuilder` — frozen dataclass, единственный источник URL-логики для надальнийвосток.рф. Endpoint paths — module-level константы, не конфиг. ADR-024. |
| `cookie_bridge.py` | Перенос Playwright-cookies в `requests.Session`. |

### `infra/sqlite/`

SQLite-адаптеры. Все SQL в репозиториях; sync sqlite3 (без aiosqlite). [[decisions/ADR-016-repository-invariants-begin-immediate]].

| Пакет/файл | Назначение |
|---|---|
| `connection.py` | `ConnectionProvider` — thread-local connections, `PRAGMA busy_timeout=5000` на каждом коннекте. |
| `init_db.py` | Инициализация схемы БД. |
| `migrations.py` | Оркестратор миграций; `migrations_v1_to_v2.py` … `v4_to_v5.py` — версионные миграции. |
| `repositories/` | По одному файлу на репозиторий: `lots.py`, `notifications.py`, `cycles.py`, `region_subscriptions.py`, `settings.py`, `smtp_credentials.py`, `state.py`, `user_state.py`. |

### `infra/parsers/`

| Файл | Назначение |
|---|---|
| `list_parser.py` | `SelectolaxListParser` — парсит `/cabinet/free-lot` HTML (tr[data-key], td[data-col-seq]). Изолирован от сети, тестируется на fixture-снапшотах. |
| `detail_parser.py` | `SelectolaxDetailParser` — парсит `/cabinet/free-lot-view` (`.request-declaration__block-main`). |

### `infra/notifiers/`

| Файл | Назначение |
|---|---|
| `registry.py` | `ExplicitNotifierRegistry` — реестр нотификаторов. [[architecture/06-notifier-registry]]. |

### `infra/smtp/`

| Файл | Назначение |
|---|---|
| `email_notifier.py` | `SmtpEmailNotifier` — отправка через SMTP, ManualSTARTTLS. ADR-021. |
| `host_policy.py` | `DefaultSmtpHostPolicy` — определяет SMTP host по домену email. [[architecture/03-protocols#SmtpHostPolicy]]. |
| `provider_catalog.py` | `StaticSmtpProviderCatalog` — таблица провайдеров (Yandex, Gmail, Mail.ru, …). |
| `constants.py` | `DEFAULT_SMTP_HOST`, `DEFAULT_SMTP_PORT` — перенесены из domain/models.py (ADR-020, ADR-024). |

### `infra/playwright/`

| Файл | Назначение |
|---|---|
| `login.py` | `PlaywrightLoginSession` — headed Chromium с persistent context, ЕСИА-авторизация. Встроен в FastAPI threadpool. |

### `infra/sse/`

SSE-подсистема (server-sent events). Sync→async мост.

| Файл | Назначение |
|---|---|
| `bus.py` | `SSEBus` — единая шина событий. Управляет подписчиками. |
| `subscriptions.py` | `SSESubscription` — per-tab subscription с `queue.Queue`. Multi-tab fan-out: один источник → N очередей. |
| `sse_stream.py` | ASGI-генератор потока: `queue.get()` через `run_in_executor` (sync→async). |
| `browser_sse_notifier.py` | `BrowserSSENotifier` — реализует `Notifier` Protocol, пушит события в SSE-шину. |

## `services/`

Application-layer use cases. Зависят только от Protocols из `domain/interfaces.py`. [[architecture/02-layers-dip]] §Layer 3.

| Файл | Назначение |
|---|---|
| `monitor_cycle.py` | `MonitorCycleService` — основной цикл мониторинга: fetch list → parse → diff → dispatch. Использует `TorgiUrlBuilder`. |
| `enrichment.py` | `EnrichmentService` — фоновое дозаполнение detail-карточек. ThreadPoolExecutor до 10 тредов. |
| `full_scan.py` | `FullScanService` — полная пагинированная выгрузка (backfill / catchup). |
| `paginated_list_fetcher.py` | `PaginatedListFetcher` — обход страниц с ранним выходом по sort=-DATE_CREATE. |
| `notifier_dispatcher.py` | `NotifierDispatcher` — очередь нотификаций, consumer_loop, идемпотентность по (lot_id, channel). |
| `onboarding.py` | `OnboardingService` — FSM onboarding flow. [[onboarding]], [[decisions/ADR-018-onboarding-fsm-server-enforced]]. |
| `settings.py` | `SettingsService` — чтение/запись пользовательских настроек (регионы, recipients). |
| `login.py` | `LoginService` — оркестрирует Playwright headed-login, управляет сессией. |
| `lot_query.py` | Запросы к репозиторию лотов (фильтрация, сортировка, пагинация). |
| `lot_user_state.py` | Обновление пользовательского состояния лота (saved/dismissed/read). |
| `filter_matcher.py` | Матчинг лотов по пользовательским фильтрам (area, region, status). |
| `view_filters.py` | Применение фильтров к view-запросу. |
| `backfill.py` | `BackfillService` — запуск полного сканирования по запросу. |
| `catchup_dismiss.py` | Dismiss исторических лотов до выбранного порога. |
| `dnd.py` | Do-Not-Disturb расписание уведомлений. |
| `smtp_test.py` | Тестовая отправка SMTP-письма (диагностика). |
| `session_expired_email.py` | Уведомление по email об истечении ЕСИА-сессии. |

### `services/diagnostics/`

| Файл | Назначение |
|---|---|
| `service.py` | `DiagnosticsService` — сбор состояния системы (БД, сессия, last cycle). |
| `exclude_policy.py` | Политика исключения чувствительных данных из диагностического отчёта. |

## `web/`

HTTP-слой (FastAPI). Не содержит бизнес-логики — делегирует в `services/`.

| Файл | Назначение |
|---|---|
| `middleware.py` | CSRF + DNS-rebinding защита (ADR-011). Pure ASGI class, без BaseHTTPMiddleware. Host-allowlist + Origin-check для state-changing методов. |
| `onboarding_gate.py` | Middleware/depends: перенаправляет незаконченный onboarding на `/onboarding`. |
| `rate_limit.py` | Rate-limiting для отдельных эндпоинтов (login, SMTP test). |
| `deps.py` | FastAPI dependencies (Container, ConfigSource и пр.). |
| `feed_context.py` | Сборка контекста для главной страницы (feed). |
| `sse_encoder.py` | Сериализация SSE-событий в JSON-фрагменты для клиента. |
| `templates.py` | Jinja2 environment factory, фильтры, глобальные функции. |
| `_helpers.py` | Вспомогательные функции для route-handlers. |

### `web/routes/`

Один router-файл на домен. Все handlers — `def` (sync), FastAPI разносит в threadpool.

| Файл | Назначение |
|---|---|
| `main.py` | Корневой роут (`/`), feed-страница. |
| `lots.py` | API лотов (`/lots/`, детали лота, lot-actions). |
| `onboarding.py` | Onboarding wizard (4 шага). |
| `settings.py` | Пользовательские настройки (регионы, recipients, фильтры). |
| `auth.py` | ЕСИА-авторизация (запуск Playwright, статус сессии). |
| `notifications.py` | Управление уведомлениями (DND, SMTP test). |
| `diagnostics.py` | Диагностический эндпоинт. |
| `events.py` | SSE-эндпоинт (`/events`). |
| `filters.py` | CRUD пользовательских фильтров. |
| `backfill.py` | Запуск backfill / catchup. |
| `catchup.py` | Catchup-dismiss эндпоинт. |
| `cycle.py` | Ручной запуск monitor cycle. |
| `dnd.py` | DND-расписание API. |

### `web/static/` и `web/templates/`

CSS/JS (HTMX-инициализация) и Jinja2 шаблоны (feed, onboarding, layout-фрагменты). Финальные файлы из дизайн-handoff (`claude-design/`).

## `auth/`

Заглушка-пакет (`__init__.py`). Логика авторизации вынесена в `infra/playwright/login.py` и `services/login.py`.

## `monitor/`

Заглушка-пакет (`__init__.py`). Логика цикла в `services/monitor_cycle.py`.

## `notifiers/`

Заглушка-пакет (`__init__.py`). Реализации нотификаторов: `infra/smtp/email_notifier.py`, `infra/sse/browser_sse_notifier.py`. Реестр: `infra/notifiers/registry.py`. [[architecture/06-notifier-registry]].

## `enrichment/`

Заглушка-пакет (`__init__.py`). Логика в `services/enrichment.py`.

## `utils/`

| Файл | Назначение |
|---|---|
| `log.py` | Структурный JSON-логгер, ротация посуточно 30 дней. |
| `log_filters.py` | Фильтры логов (подавление чувствительных данных). |
| `log_level.py` | Утилиты управления уровнем логирования. |

> Пути данных (`platformdirs`) вынесены в `infra/` (нет отдельного `utils/paths.py` — поиск по коду).

## `autostart/`

Заглушка-пакет (`__init__.py`). Кросс-платформенный автозапуск (Task Scheduler / XDG Autostart) — pending bd-задача a4t.9.

## `db/`

Заглушка-пакет (`__init__.py`). Вся SQL-логика в `infra/sqlite/`.

## Конфигурация и данные (вне `src/`)

Пути через `platformdirs`:

| Файл/папка | Windows | Linux |
|---|---|---|
| `config.json` | `%LOCALAPPDATA%\fis-monitor\` | `~/.config/fis-monitor/` |
| `state.db`, `profile/`, `logs/` | `%LOCALAPPDATA%\fis-monitor\` | `~/.local/share/fis-monitor/` |

```
state.db                    # SQLite WAL — зеркало лотов + user state
profile/                    # Playwright persistent context (ЕСИА cookies)
logs/
  app.jsonl
  requests.jsonl
```

## Тесты (`tests/`)

| Папка | Содержимое |
|---|---|
| `tests/fixtures/` | Датированные HTML-снапшоты сайта для парсер-тестов. |
| `tests/unit/` | Юнит-тесты: парсеры, diff, filter_matcher, сервисы (без сети и БД). |
| `tests/infra/` | Инфра-тесты с реальным SQLite (без сети). |
| `tests/fakes/` | Реализации фейков для Protocol-интерфейсов (typed, mypy --strict). |

## Staging (`tools/fake_torgi/`)

Локальный двойник надальнийвосток.рф для ручной проверки end-to-end сценариев. Подробнее: [[staging-fake-site]].

## См. также

- [[architecture/02-layers-dip]] — слои, DIP, границы между пакетами
- [[architecture/04-composition-root]] — порядок сборки зависимостей в `build_container()`
- [[architecture/03-protocols]] — каталог Protocol-интерфейсов
- [[architecture/06-notifier-registry]] — архитектура нотификаторов
- [[decisions/ADR-016-repository-invariants-begin-immediate]] — инварианты репозиториев
- [[decisions/ADR-018-onboarding-fsm-server-enforced]] — onboarding FSM
- [[decisions/ADR-024-target-config-and-url-builder]] — TargetConfig + TorgiUrlBuilder
- [[onboarding]] — пользовательский flow onboarding
