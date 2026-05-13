# Глоссарий

Словарь терминов проекта. Если термин раскрыт подробнее в отдельном документе — даётся ссылка.

## Государственные системы и право

- **ЕСИА** — Единая система идентификации и аутентификации (Госуслуги). Через неё происходит вход на ФИС. См. [[web/authentication]].
- **ФИС** — Федеральная информационная система. В нашем контексте — сайт `НаДальнийВосток.рф` (`xn--80aaggvgieoeoa2bo7l.xn--p1ai`). См. [[product/site-architecture]].
- **ОГВ** — Орган государственной власти. Юрлицо, выдающее лот (региональное министерство, департамент имущества и т.п.).
- **ВРИ** — Вид разрешённого использования земельного участка (ИЖС, ЛПХ, сельхоз и т.д.).
- **ПКК** — Публичная кадастровая карта Росреестра.
- **ЕГРН** — Единый государственный реестр недвижимости. Источник кадастровых данных.
- **ДМС** — Градусы-минуты-секунды. Формат координат на сайте ФИС (например, `43°07'12.0"N`).
- **ПДн** — Персональные данные. Регулируются 152-ФЗ. См. [[product/risks-legal]].
- **СНИЛС** — Страховой номер индивидуального лицевого счёта. Иногда фигурирует в профиле ЕСИА.

## Архитектура мониторинга

- **lazy enrichment** — Фоновое дозаполнение деталей лотов после первичного обнаружения в списке. Список парсится быстро, детали карточки тянутся отдельным воркером. См. [[product/monitoring-plan]].
- **mirror table** — Таблица БД, отражающая данные сайта. Можно стереть и перезалить из источника без потери для пользователя (например, `lots`).
- **user-state table** — Таблица БД с пользовательскими данными, которые нельзя терять: отправленные уведомления, `last_known_id`, настройки. См. [[db/schema|db/schema.sql]].
- **early-exit** — Алгоритм обхода ленты: сортировать по `DATE_CREATE DESC`, останавливаться на первом известном ID. См. [[parser/sort-strategy]].
- **monitor-cycle** — Один проход мониторинга: получить первую страницу, найти новые ID, поставить их в enrichment.

## Веб-стек и интеграция

- **PJAX** — Техника частичного обновления страниц через `X-PJAX` header (используется Yii2 на стороне сайта). Возвращает фрагмент HTML вместо полной страницы.
- **persistent context** — Режим Playwright, сохраняющий cookies и localStorage между запусками в директории `profile/`. Используется для удержания сессии ЕСИА.
- **sticky-session** — Закрепление сессии за конкретным upstream-сервером балансировщиком. На стороне ФИС не подтверждено, но термин встречается в обсуждениях. У нас не применяется.
- **SSE** — Server-Sent Events. Однонаправленный поток обновлений от FastAPI к браузеру (новые лоты, статус цикла). См. [[web/ui-architecture]].
- **CSRF** — Cross-Site Request Forgery. Защита через проверку `Origin`-header + secure-cookie токен. См. [[architecture]] → §1 CSRF middleware и [[decisions-log]].

## Уведомления и инфраструктура

- **Notifier** — Абстрактный плагин уведомлений (email, browser, в будущем Telegram). См. [[notifications]].
- **бот-ящик / bot-mailbox** — Выделенный SMTP-аккаунт приложения (Yandex с app-password), используемый по умолчанию для отправки email. Клиент может переопределить SMTP в панели.
- **single-instance lock** — Защита от двух одновременно запущенных копий через PID-файл `{data_dir}/app.lock` (Windows: `%LOCALAPPDATA%\fis-monitor\`, Linux: `~/.local/share/fis-monitor/`, через `platformdirs`). См. [[product/monitoring-plan]] → «Защита от двух копий».
- **heartbeat** — Опциональная периодическая «сводка-я-жив» в выбранный канал. По умолчанию выключена.

## Domain — модели и diff

- **compute_changes** — чистая функция `compute_changes(old: Lot | None, new: Lot, tracked: Sequence[TrackedField]) -> list[FieldChange]` в `domain/diff.py`. Вызывается репозиторием внутри `BEGIN IMMEDIATE` tx (ADR-016, R3-C2) — закрывает TOCTOU между `SELECT old` и `UPDATE`. Без I/O, полностью детерминирована.

- **FieldChange / LotUpsertResult** — diff-протокол репозитория. `FieldChange.field` ограничен `TrackedField` Literal — SQL-identifier-инъекции исключены на уровне типа. Инвариант `LotUpsertResult`: `was_new=True ⇒ changes=[]` (history не пишется для новых лотов). См. [[data-model/lot]], [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]].

- **TrackedField** — `Literal["status", "area_sqm", "date_update", "auction", "is_active", "list_presence"]`. Whitelist полей для tracking в `lots_history`. `ALLOWED_TRACKED_FIELDS` в `domain/diff.py` деривируется через `typing.get_args(TrackedField)` — SSOT, дрейф Literal ↔ frozenset невозможен. См. [[decisions/ADR-022-allowed-tracked-fields-ssot-smtp-policy-error|ADR-022]].

- **LotPublicDTO vs LotUserDTO** — `LotPublicDTO` публикуется через EventBus (без `user-state`). `LotUserDTO` возвращается в server-rendered HTML или через `GET /api/lots/{id}/user-state` (добавляет `starred`, `submitted`, `note`). `raw_json` исключён из обоих через `@model_serializer`. Разделение — forward-compat с multi-user v3. См. [[data-model/lot]], [[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]].

- **ResolvedSmtpEndpoint** — `@dataclass(frozen=True, slots=True)` с полями `ip`, `family`, `port`, `original_host`. Результат `SmtpHostPolicy.resolve_and_check()`. `ip` используется для TCP-connect (pin, закрывает TOCTOU), `original_host` — для SNI и TLS-cert verification. Не Pydantic-модель: infra-internal, не сериализуется. См. [[data-model/notifications]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-021-manual-starttls-connect-by-ip|ADR-021]].

- **SmtpHostPolicy** — `Protocol` с методом `resolve_and_check(host, port) -> ResolvedSmtpEndpoint`. `DefaultSmtpHostPolicy` — реализация с injectable resolver (тестируема без DNS). Fail-closed: ошибка при любом адресе из `getaddrinfo`, попавшем в blocklist (DNS-rebinding multi-record). DNS resolve выполняется вне любой БД-транзакции (R4-M2). См. [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-022-allowed-tracked-fields-ssot-smtp-policy-error|ADR-022]].

- **SsePayloadSchema** — whitelist полей по типу SSE-события для persist critical-event в таблицу `state` и для redaction при `logger.warning`. `for_event()` fail-closes к пустому `frozenset` при неизвестном типе. Поля вне whitelist вырезаются перед записью — defence-in-depth против утечки PII (stacktrace, recipient, smtp_response). См. [[data-model/sse]], [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] R3-C5.

- **DiagnosticsExcludePolicy** — Pure-function policy class (`services/diagnostics/exclude_policy.py`) определяющий какие поля исключить или redact'ить при сборке `diagnostic.zip`. SSOT для PII-surface: `EXCLUDED_SETTINGS_PATHS`, `EXCLUDED_DB_FIELDS`, `REDACTED_DB_FIELDS`. Потребляется `DiagnosticsService` (a4t.7). См. [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]], [[data-model]].

## Инфраструктура SQLite

- **ConnectionProvider** — `infra/sqlite/connection.py`, Layer 0. Обеспечивает per-thread `sqlite3.Connection` через `threading.local()`. Применяет per-connection PRAGMA (ADR-007) при каждом `_open()`: `auto_vacuum`, `journal_mode`, `synchronous`, `foreign_keys`, `busy_timeout`, `temp_store`, `cache_size`, `mmap_size`. Регистрация активных соединений через `dict[id, conn]` под `threading.Lock`; `close_all()` делает snapshot перед закрытием (защита от RuntimeError). Не является domain Protocol — принимается репозиториями конкретным типом. См. [[architecture/03-protocols]], [[decisions/ADR-007-per-connection-pragma|ADR-007]].

- **MigrationRequired** — `DomainError` в `domain/errors.py`. Поднимается `init_db()` когда `PRAGMA user_version < latest_version` и `migration_runner` не передан. Атрибуты: `from_version: int`, `to_version: int`. Сообщение содержит только номера версий — без путей файлов (PII-safe). Composition root обязан либо поймать и запустить `migration_runner`, либо показать человекочитаемое сообщение. Реализовано в akv.2; конкретный runner — akv.3. См. [[architecture/03-protocols]] §3.1.

- **init_db()** — функция в `infra/sqlite/init_db.py`. Pre-flight guard при старте: читает `PRAGMA user_version` вне транзакции, затем по алгоритму: `current==0` + нет таблиц → `executescript(schema_sql)`; `current==latest` → no-op; `current>latest` → `RuntimeError` (downgrade); `current<latest` → вызвать `migration_runner` или `raise MigrationRequired`. Зависит только от `ConnectionProvider` (infra-internal) — domain не знает о sqlite3. Реализовано в akv.2. См. [[decisions/ADR-007-per-connection-pragma|ADR-007]].

- **v1_to_v2 (migration)** — конкретная функция миграции схемы SQLite с `user_version=1` на `user_version=2`. Реализована в `infra/sqlite/migrations_v1_to_v2.py`. Часть A: `notifications` — 12-step rebuild pattern (CREATE _new + INSERT SELECT + DROP old indexes + DROP + RENAME + CREATE indexes). Все v1-строки трактуются как успешные отправки: `status='sent'`, `attempt_no=1`, `last_attempt_at=sent_at`. Часть B: `smtp_credentials` — `ALTER TABLE ADD COLUMN` для `smtp_host`/`smtp_port` (ADR-020). Зарегистрирована через `MIGRATION_V1_TO_V2 = Migration(1, 2, apply=v1_to_v2)` и фабрику `default_migration_runner()`. Функция работает ВНУТРИ BEGIN IMMEDIATE runner-а: не открывает свою tx, не ставит `PRAGMA user_version`. FK-toggling опущен — `notifications` не имеет FK-ссылок. Реализовано в akv.3. См. [[decisions/ADR-019-notification-state-machine]], [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]].

## Protocol-швы (domain/interfaces.py)

- **LotRepository** — `Protocol` (Layer 1) для persist лотов. Ключевые методы: `upsert(lot, *, tracked)` (атомарный BEGIN IMMEDIATE + compute_changes внутри tx), `mark_seen`, `mark_inactive`, `needing_enrichment`. Реализация: `SqliteLotRepository`. См. [[architecture/03-protocols]], [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]].

- **NotificationsRepository** — `Protocol` (Layer 1) для state machine доставки уведомлений. PK `(lot_id, channel, recipient)`. Методы: `reserve → mark_attempt → mark_sent | mark_permanent_fail | list_pending_older_than`. Каждый метод в своей короткой `BEGIN IMMEDIATE` tx. Возврат `mark_attempt → int | None` (R4-C4: None при race с финальным статусом). См. [[decisions/ADR-019-notification-state-machine|ADR-019]], [[notifications]].

- **EventSubscription[T]** — generic `Protocol` (context-manager handle) результата `EventBus.subscribe()`. Методы: `iter() -> Iterator[T]`, `unsubscribe()` (idempotent). Перенесён из `models.py` (follow-up z9d). Python 3.12 type parameter syntax (`class EventSubscription[T]`). См. [[architecture/03-protocols]].

- **ThreadEventBus** — конкретная реализация `EventBus` Protocol (`infra/sse/bus.py`). Sync→async bridge для SSE fan-out. Один `queue.Queue(maxsize=100)` на подписчика. Маршрутизация по `event.priority` ClassVar: `normal` → `put_nowait` + drop-from-tail; `critical` → blocking `put(timeout=2.0)` + force-unsubscribe slow consumer. Per-type in-memory слоты `_last_critical: dict[type, SseEvent]` для replay при SSE reconnect — доступны через `bus.last_critical(EventClass)`. **Без persistence в БД** (ADR-008): слоты живут в памяти, F5 восстанавливает из source of truth. Единый `threading.Lock` защищает список подписчиков и слоты. Реализовано в tic.1. **MVP scope: in-memory only; state-table persistence с TTL=1h (ADR-008 R3-C5) — planned, see [[#12y]].** См. [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]], [[architecture/07-concurrency]] §7.3.

- **MigrationRunner** — `Protocol` (Layer 2) в `domain/interfaces.py`. Методы: `list_migrations() -> Sequence[Migration]`, `run_pending(conn, from_version, to_version)`, `__call__(conn, from_version, to_version)` (callable seam совместимый с `init_db.migration_runner` параметром). Контракт: первая операция — `BEGIN IMMEDIATE`, затем re-check `PRAGMA user_version == from_version` (TOCTOU defence, закрывает follow-up `1zk`); миграции и `PRAGMA user_version = N` — внутри той же tx. `conn`/`Sequence` параметры типизированы `Any` чтобы domain не зависел от sqlite3. Реализовано в akv.4. См. [[architecture/03-protocols]].

- **SqliteMigrationRunner** — конкретная реализация `MigrationRunner` Protocol в `infra/sqlite/migrations.py`. DI: `__init__(migrations: Sequence[Migration] = ())`. Greedy chain-build по `from_version`. Greenfield-MVP реальные prod-DB прыгают 0→2 через `schema.sql` (закрыто akv.2), runner стартует пустым; v1→v2 rebuild migration будет зарегистрирована в akv.3. На любом исключении внутри `run_pending` — `conn.rollback()` + re-raise. Реализовано в akv.4.

- **Migration** — `dataclass(frozen=True, slots=True)` в `infra/sqlite/migrations.py`. Поля: `from_version: int`, `to_version: int`, `apply: Callable[[sqlite3.Connection], None]`. Инвариант: `apply` запускается ВНУТРИ runner's `BEGIN IMMEDIATE` — НЕ открывает свою tx, НЕ вызывает `PRAGMA user_version` (runner сам ставит после успешного применения). Реализовано в akv.4.

- **ConcurrentMigrationError** — `DomainError` в `domain/errors.py`. Поднимается `SqliteMigrationRunner.run_pending` когда после `BEGIN IMMEDIATE` обнаружено `PRAGMA user_version != expected_from_version` (race: другой процесс/worker обновил схему между init_db read и захватом writer-lock). Атрибуты: `expected_version: int`, `actual_version: int` — PII-safe. Defence-in-depth даже при single-instance lock. Закрывает follow-up bd `1zk`. Реализовано в akv.4.

- **MigrationChainBroken** — `DomainError` в `domain/errors.py`. Поднимается когда в registered migrations нет непрерывной цепочки `from_version → to_version`. Атрибуты: `from_version: int`, `to_version: int`. Configuration-level ошибка (не runtime race). Реализовано в akv.4.

- **SqliteSettingsRepository** — конкретная реализация `SettingsRepository` Protocol в `infra/sqlite/repositories/settings.py`. K/V на таблице `state`. Write-методы (`set`, `set_onboarding`) — внутри `BEGIN IMMEDIATE` + ROLLBACK on exception. Read-методы (`get`, `get_onboarding`) — без явной tx. `OnboardingState` сериализуется как `.value` под ключом `onboarding_state`; дефолт `OnboardingState.NOT_STARTED` если строки нет. `updated_at` стампится через injected `Clock.now()` (UTC, ISO 8601). Реализовано в akv.7.

- **SqliteSmtpCredentialsRepository** — конкретная реализация `SmtpCredentialsRepository` Protocol в `infra/sqlite/repositories/smtp_credentials.py`. Singleton row `id=1` (enforced через `CHECK (id=1)` в schema). `save()` — атомарный `INSERT OR REPLACE` всех 6 полей (smtp_user, smtp_password, smtp_host, smtp_port, use_default, updated_at) в одной `BEGIN IMMEDIATE` tx — последний save wins consistently. `load()` использует **named columns** в SELECT (обходит column-order quirk после `v1_to_v2` migration — см. [[#v1_to_v2 (migration)]]), оборачивает `smtp_password` в `SecretStr`. `.get_secret_value()` вызывается строго один раз — в момент SQL binding, никогда в logs/exceptions/repr (ADR-017). Реализовано в akv.7.

- **tmp_db (pytest fixture)** — function-scope в `tests/conftest.py`. Возвращает `ConnectionProvider` подключённый к свежему `tmp_path/state.db` с применённым `schema.sql` через `init_db()`. WAL-режим выставляется per-connection (`ConnectionProvider._configure`, ADR-007). Cleanup: `provider.close_all()` в `finally`. Companions: `tmp_db_path` (Path) и `schema_sql` (session-scoped str). Реализовано в vgm.1.

## Onboarding и конфигурация

- **OnboardingState** — `StrEnum` с пятью состояниями: `not_started → regions_set → smtp_configured → recipients_set → completed`. Server-side FSM, хранится в таблице `state` под ключом `onboarding_state`. Transitions валидирует `OnboardingService.advance()` через `BEGIN IMMEDIATE`. См. [[onboarding]], [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]].

- **NotifierConfig** — базовый Pydantic `BaseModel` для конфиг-схем плагин-каналов уведомлений. Конкретные классы: `EmailNotifierConfig`, `BrowserNotifierConfig`, `HeartbeatNotifierConfig`. Используется composition root для инициализации explicit registry (ADR-002). См. [[notifications]], [[decisions/ADR-002-plugin-discovery-explicit-registry|ADR-002]].

- **import-linter (контракты архитектуры)** — инструмент статического анализа импортов, закреплённый в `dev`-зависимостях (`pyproject.toml`). Конфигурация в `.importlinter` в корне репо. Два контракта: `layers` — слоистая архитектура (composition/app → web → services → infra → domain, нарушение идёт снизу вверх), `domain_purity` — domain не импортирует инфра-библиотеки (sqlite3, requests, fastapi, playwright, smtplib). Запуск: `lint-imports`. Тест: `tests/test_import_linter_contracts.py`. Слои `composition` и `app` объявлены опциональными `(...)` до реализации в тасках `8ov.1`/`8ov.2`. См. [[decisions/ADR-006-import-linter-ci]].

## См. также

- [[decisions-log]]
- [[product/mvp-scope]]
- [[product/site-architecture]]
- [[web/authentication]]
