# Журнал решений (MOC)

Этот файл — **stub-MOC** (Map of Content). Атомарные ADR живут в `docs/decisions/`. Здесь только оглавление + ссылки на ранние решения по доменам (раздел ниже).

> Дата фиксации канона: 12.05.2026. Реструктуризация в атомарные ADR: 13.05.2026.

## ADR-блоки (после ревью архитектуры)

**Структура и сборка:**
- [[decisions/ADR-001-notifier-protocol-not-abc|ADR-001]] — Notifier — Protocol, не ABC
- [[decisions/ADR-002-plugin-discovery-explicit-registry|ADR-002]] — Plugin discovery — explicit registry, не entry_points
- [[decisions/ADR-004-composition-root-container-infra-services|ADR-004]] — Composition root — самописный Container, разделённый на Infra/Services
- [[decisions/ADR-006-import-linter-ci|ADR-006]] — import-linter в CI

**Конкурентность, lifespan, БД:**
- [[decisions/ADR-005-concurrency-soft-yield-retry-busy|ADR-005]] — Concurrency — soft-yield, retry SQLITE_BUSY, без unified writer-queue
- [[decisions/ADR-007-per-connection-pragma|ADR-007]] — Per-connection PRAGMA vs persistent
- [[decisions/ADR-014-two-phase-shutdown|ADR-014]] — Two-phase shutdown policy
- [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]] — Repository invariants — BEGIN IMMEDIATE + identifier whitelist + private _sync_geo

**Уведомления и события:**
- [[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]] — Error strategy — Exception для всего, Result только для Notifier
- [[decisions/ADR-008-eventbus-dual-circuit-no-db-persistence|ADR-008]] — EventBus — двухконтурный (normal/critical), без persistence в БД
- [[decisions/ADR-019-notification-state-machine|ADR-019]] — Notification state machine — reserve → attempt → sent | permanent_fail

**Безопасность:**
- [[decisions/ADR-010-data-dir-location-policy|ADR-010]] — Data_dir location policy
- [[decisions/ADR-011-dns-rebinding-host-allowlist|ADR-011]] — DNS-rebinding защита — strict Host allow-list
- [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]] — Diagnostic.zip — explicit allow-list + redactor
- [[decisions/ADR-013-locker-os-level-pid-info-only|ADR-013]] — Locker — OS-level lock, PID info-only
- [[decisions/ADR-015-smtp-host-validation|ADR-015]] — SMTP host validation — IP/DNS rules + resolve-recheck
- [[decisions/ADR-017-secrets-secretstr-crash-dump-exclusion|ADR-017]] — Secrets handling — SecretStr + crash-dump exclusion
- [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]] — Onboarding FSM server-enforced
- [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]] — SMTP host/port SSOT = state.db
- [[decisions/ADR-021-manual-starttls-connect-by-ip|ADR-021]] — Manual STARTTLS — обход smtplib server_hostname bug
- [[decisions/ADR-022-allowed-tracked-fields-ssot-smtp-policy-error|ADR-022]] — ALLOWED_TRACKED_FIELDS SSOT + SmtpHostPolicyError наследует UpstreamError
- [[decisions/ADR-023-configsource-save-extension|ADR-023]] — `ConfigSource.save()` расширяет существующий Protocol (без отдельного SettingsWriter)
- [[decisions/ADR-024-target-config-and-url-builder|ADR-024]] — `TargetConfig` + `TorgiUrlBuilder`: config seam для реального target URL (`надальнийвосток.рф`); SMTP defaults → `infra/smtp/constants.py`
- [[decisions/ADR-025-sse-single-endpoint|ADR-025]] — SSE routing: единственный роут `/events`, фильтрация по `sse-swap` на клиенте
- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — Distribution packaging: PyInstaller --onedir + bundled Chromium, build-on-target strategy
- [[decisions/ADR-027-silent-cookie-refresh|ADR-027]] — Silent cookie refresh: headless Playwright `silent_refresh()` для продления сессии без интерактивного логина (shared `_lock`, 30s deadline, `needs_manual_login` enum value)
- [[decisions/ADR-028-paginated-catalogue-backfill|ADR-028]] — Paginated catalogue backfill: `PaginatedListFetcher` + `BackfillService` + auto-trigger heuristic (empty DB) + single-flight across auto/manual + notify caller-side
- [[decisions/ADR-029-vendor-htmx-no-cdn|ADR-029]] — Vendor htmx locally (no CDN for JS assets): supply-chain mitigation F-03; htmx 1.9.12 + ext/sse.js in `static/vendor/htmx-1.9.12/`
- [[decisions/ADR-030-sse-lot-new-dispatcher-ssot|ADR-030]] — SseLotNew dedup: Dispatcher SSOT for SSE channel — убрана прямая публикация из `MonitorCycleService`; единственный путь — через `BrowserSseNotifier`
- [[decisions/ADR-031-region-ssot-site-id|ADR-031]] — Region SSOT site-id: `SUBJECTS_BY_MACRO` + `SUBJECT_TITLE_BY_ID` в `domain/regions.py`; URL param переключён с `rfSubjectId` на `region=`; новое поле `Settings.subject_site_ids` для fetch-scope
- [[decisions/ADR-032-onboarding-driven-backfill|ADR-032]] — Onboarding-driven backfill: auto-trigger перенесён из lifespan в `_handle_step4_next` (completion handler); supersedes ADR-028 §Auto-trigger
- [[decisions/ADR-033-web-editable-schedule|ADR-033]] — Web-editable schedule: `POST /settings/schedule` (единый payload); hot-reload через ConfigSource; `MonitorCycleService` и `FullScanService` читают `.current()` каждую итерацию
- [[decisions/ADR-034-cookie-bridge-playwright-requests|ADR-034]] — Cookie bridge Playwright → requests.Session: `CookieStore` Protocol + `RequestsCookieStore`; `_export_cookies()` после каждого успешного login/refresh; `SessionExpiredError` по title в `SelectolaxListParser`
- [[decisions/ADR-035-three-scope-filter-model|ADR-035]] — Three-scope filter model: Fetch (macro-region URL) / Notify (`filters.rf_subjects`) / View (cookie `view_filters`); удаление мёртвого поля `Settings.subject_site_ids`; migration shim `subject_site_ids → rf_subjects`; supersedes ADR-031 §Q3 + Addendum
- [[decisions/ADR-036-head-poll-cycle-policy|ADR-036]] — Head-poll cycle policy: `MonitorCycleService` делает head-poll page=1/per_page=20 каждый `interval_minutes`; `FullScanService` — полная пагинация per_page=50 для active-set; `BackfillService` — полная пагинация при первом логине. `PaginatedListFetcher.iterate` получает kwargs `per_page` и `max_pages` (bd `gektar_monitor-3pw`)
- [[decisions/ADR-037-tls-russian-trusted-ca-bundle|ADR-037]] — TLS hardening: `verify=False` → `verify=russian_trusted_ca_bundle()` (bundled PEM с Russian Trusted Root CA + Sub CA); fail-closed RuntimeError при отсутствии bundle; убран `urllib3.disable_warnings`; supersedes ADR-024 §TLS-note
- [[decisions/ADR-038-smtp-provider-catalog|ADR-038]] — SMTP provider catalog: `SmtpProviderCatalog` Protocol (domain) + `StaticSmtpProviderCatalog` (infra hardcoded dict) + `GET /settings/smtp/suggest?email=` endpoint; UI prefill host/port/use_starttls по домену email + app-password hint для Gmail/Outlook/Yandex; БД-схема `SmtpCredentials` не меняется; suggestion НЕ обходит `DefaultSmtpHostPolicy` (bd `gektar_monitor-0bf`)
- [[decisions/ADR-039-subscribed-at-region-cutoff|ADR-039]] — subscribed_at per-region cutoff: новая таблица `region_subscriptions(region_id PK, subscribed_at)` в state DB; filter в `notifier_dispatcher.dispatch` на domain-уровне (`date_create < subscribed_at` → intentional suppression); migration в `WatchdogConfigSource._do_reload` diff old/new (set-if-absent для net-new регионов, delete для удалённых); `RegionSubscriptionRepository` Protocol; at-least-once SLO сохраняется (suppression задокументирована в ADR-019)
- [[decisions/ADR-040-egrn-registration-date|ADR-040]] — EGRN registration date: новое поле `lots.date_registry TIMESTAMP NULL` + `Lot.date_registry` / `ParsedDetail.date_registry`; парсинг ключа «Дата постановки на учет» с detail-страницы; обе даты в карточке (date_create = ФИС, date_registry = ЕГРН); migration v4→v5; `date_registry` не в `TrackedField` (bd `gektar_monitor-svqi`)
- [[decisions/ADR-041-test-tactics-amendment|ADR-041]] — Test tactics amendment: wiring→Layer 5, log parametrize-collapse (≤120 LOC), no sqlite3 in unit/services, pyramid baseline (non-binding by file count), canonical fake в tests/fakes/
- [[decisions/ADR-042-toggle-archive-submitted-semantic-overload|ADR-042]] — toggle_archive semantic overload: `submitted` column reused for "archived" UX concept; Option A (document + no schema change) accepted at P4; Option B (split column) deferred until dual-flag product need

**Резервирование:**
- [[decisions/ADR-009-backup-user-state-tables-only|ADR-009]] — Backup стратегия — только USER_STATE_TABLES

---

## Решения по доменам (раннее обсуждение)

Зафиксированы 12.05.2026. Содержат ранние продуктовые/инфраструктурные решения, поверх которых легли ADR'ы выше.

### Что в MVP

| # | Решение |
|---|---|
| ✅ | **Каталог НЕ в MVP** (v2). Архитектурно заложить расширяемость на каталог в схеме БД и API |
| ✅ | **Telegram НЕ в MVP** (v2 через панель плагинов). Архитектурно заложить плагин-систему |
| ✅ | **Heartbeat-сводка опционально** — чекбокс в настройках, по умолчанию выключено |
| ✅ | **Auto-update полностью убран**. Никакой подписи манифеста, GitHub Releases, ничего. Обновления — архивом руками |
| ✅ | **Lazy enrichment** при первом запуске — качаем только первую страницу, дальше обычным циклом |

### Парсинг и сервер

| # | Решение |
|---|---|
| ✅ | `per-page=10`, окончательно везде в коде и docs |
| ✅ | Сортировка `sort=-DATE_CREATE`, ранний выход на знакомом id |
| ✅ | Интервал между циклами — **настройка пользователя** в панели, диапазон **0..60 минут**, дефолт 15. Значение `0` = непрерывный режим, следующий цикл стартует сразу после завершения предыдущего |
| ✅ | **Фильтры — только 2**: `regions` (макрорегион 1=ДФО, 2=Арктика) и `rf_subjects` (субъекты РФ). Площадь, категория, ВРИ — не фильтруем |
| ✅ | **Семантика фильтров**: в БД храним **все лоты** с выбранных макрорегионов (`regions` — fetch-time, ограничено URL сайта). Фильтр `rf_subjects` применяется **только к уведомлениям** (notify-time), на отображение в UI можно навешивать любые ad-hoc фильтры поверх. Прошлое не теряем при изменении фильтра |
| ✅ | Час пояс хранения: **МСК**, опция в админ-панели «изменить TZ отображения» |
| ✅ | **Защита от смены ID-схемы** — проверка max(id_new_page) >= last_known_id между циклами, см. [[product/monitoring-plan]] |

### Уведомления

| # | Решение |
|---|---|
| ✅ | В MVP — только **браузер** (Notification API + SSE) и **email** |
| ✅ | **Email-источник: наш бот-ящик** + дефолтный SMTP в config. Клиент через панель может **переопределить** SMTP (host/port/login/password) если хочет свой |
| ✅ | Список получателей email — задаётся в панели, можно несколько |
| ✅ | Плагин-архитектура уведомлений — да (для будущего расширения) |

### Безопасность и операции

| # | Решение |
|---|---|
| ✅ | Bind **только на `127.0.0.1`**, не на 0.0.0.0 |
| ✅ | CSRF: проверка `Origin`-header + secure-cookie токен |
| ✅ | **Single-instance**: lock-файл в `%LOCALAPPDATA%\fis-monitor\app.lock` с PID |
| ✅ | **Hosts whitelist для Playwright**: только `xn--80aaggvgieoeoa2bo7l.xn--p1ai` и `esia.gosuslugi.ru` |
| ✅ | Pin Playwright/Chromium версии в `requirements.txt` |
| ✅ | Auto-update откл — обновления Chromium при выпуске нового exe |
| ✅ | **Idempotency notifier**: PK `notifications(lot_id, channel, recipient)`, все NOT NULL. `sent_at` — audit-колонка, НЕ часть ключа. `recipient='local'` для browser/heartbeat (NULL ломает UNIQUE). `INSERT OR IGNORE` на retry. При частичном провале (alex доставлен, wife — нет) — retry только для непровавшихся recipient'ов |
| ✅ | **SQLite concurrency**: `PRAGMA busy_timeout=5000` на каждом коннекте. `full_scan` коммитит **батчами по 50 строк**, не одной транзакцией (отпускает write-lock между батчами). **Один SQLite-коннект на поток** (`sqlite3.connect()` не thread-safe для шаринга). Единый writer-lock на уровне Python НЕ нужен — busy_timeout достаточно |
| ✅ | **Full_scan в приоритетной очереди**: приоритет 3, ниже monitor_cycle (1) и lazy_enrichment (2). Все три пишут в одну БД, разруливаются busy_timeout |
| ✅ | **session_expired флаг** проверяется на ВХОДЕ каждой background-таски (monitor_cycle, lazy_enrichment, full_scan). Если поднят — таска делает no-op и спит до сброса. Уведомление о сессии — **идемпотентно** через notifications-таблицу (ключ на «эпизод истечения»). Сбрасывается флаг после успешного `POST /auth/refresh` |
| ✅ | Lock между monitor-cycle и enrichment-worker: единая очередь, приоритет: мониторинг > enrichment > full_scan |
| ✅ | Reload конфига без рестарта: file-watch на `config.json`. **Применяется к следующему циклу, не прерывает текущий**. UI показывает баннер `applying...` до завершения цикла |

### Хранение

| # | Решение |
|---|---|
| ✅ | Гибрид-схема: колонки + JSON-blob + FTS5 + R-tree (см. `db/schema.sql`) |
| ✅ | `id INTEGER PRIMARY KEY` = data-key сайта |
| ✅ | `cadastral_no` — НЕ UNIQUE, просто INDEX |
| ✅ | Raw HTML карточек хранить (gzip, ~30 МБ через год) |
| ✅ | История изменений: только `status, area_sqm, date_update, auction` |
| ✅ | Разделить таблицы на **mirror** (можно стереть) и **user-state** (не теряем) |
| ✅ | `parser_version` в каждой строке, lazy reparse при апгрейде |
| ✅ | Шифрование БД НЕ нужно (данные публичные) |

### Логи и наблюдаемость

| # | Решение |
|---|---|
| ✅ | Структурные JSON-логи в файлы (ротация посуточно, 30 дней) |
| ✅ | Таблица `cycles` в БД: `id, started_at, finished_at, status, lots_fetched, new_lots, error` |
| ✅ | Журнал запросов — отдельный файл `requests.jsonl` (не в БД) |
| ✅ | Self-diagnostic export через панель — zip с логами + state.db (без секретов) |

### Forward-compat с хостингом

| # | Решение |
|---|---|
| ✅ | Архитектурно поддерживать переезд на VPS — переменная окружения `MODE=local\|server` |
| ⏳ | Мульти-юзер, изоляция cookies, регистрация оператора ПДн — отдельный проект v3 |

### Технологический стек

Зафиксировано 12.05.2026 после ответов на 6 блокирующих вопросов аудита.

| # | Решение |
|---|---|
| ✅ | **Pydantic v2** — для config.json и NotifierConfig (схемы плагинов) |
| ✅ | **sqlite3 sync** (встроенный) — без `aiosqlite`. Минимум зависимостей |
| ✅ | **requests** (sync) — простой HTTP-клиент. Параллельность enrichment через `ThreadPoolExecutor` (до 10 тредов) |
| ✅ | **selectolax** — HTML-парсер. Верифицирован на фикстурах: парсит таблицу списка (`table.kv-grid-table tbody tr[data-key]`) и детальную карточку (`div.request-domain__key-value`) корректно. Ограничение: нет `:scope`-селектора (обход через `.iter()`) |
| ✅ | **`playwright==1.58.0`** + Chromium 145.0.7632.6 (Chrome for Testing). Релиз 2026-01-30, 3.5 месяца в проде |
| ⚠️ | **Fallback Playwright 1.56.0** — если ЕСИА залогируется и CfT-сборку 1.58 будет flag-ать антифрод. Зафиксировать в [[ops/runbook]] как известную точку отказа |
| ✅ | **CSRF: своя минимальная middleware** — `Origin` + `Host` + `X-CSRF-Token` (cookie). ~30 строк, без внешних библиотек |
| ✅ | **SSE: `sse-starlette`** — готовый `EventSourceResponse` с keep-alive |
| ✅ | **Режим FastAPI: sync** — handlers как `def ...` (не `async def`). FastAPI сам разносит по threadpool. Цена: отказ от async-преимуществ; плюс: меньше зависимостей, проще код |
| ✅ | **Python 3.12+** |
| ✅ | **SMTP-пароль хранится в `state.db`** (таблица user-state, не в `config.json`). ПК клиента — доверенная среда, ACL `%LOCALAPPDATA%` достаточно. Pydantic-схема `config.json` НЕ содержит поле `smtp_password` |
| ✅ | **Playwright — embedded в FastAPI threadpool**. Не subprocess. Sync API + по экземпляру `Playwright()` на поток (не шарить между потоками). Используется только для headed-логина по кнопке (раз в 3 часа), не для рутинного скрейпинга |
| ✅ | **SSE мост sync→async**: `queue.Queue` (thread-safe) на процесс. Sync background-таски кладут события через `q.put()`. Async SSE-generator читает через `await loop.run_in_executor(None, q.get)` и `yield`-ит подписчикам. **Multi-tab fan-out**: один источник → N очередей подписчиков |
| ✅ | **Onboarding-gate: redirect**, не overlay. Middleware на каждом GET проверяет `state.onboarded`. Не пройден → 302 на `/onboarding?step=1`. Background-задачи стартуют, но `monitor_cycle` и `full_scan` — no-op при пустом `regions`. После завершения wizard'а → 302 на `/` |
| ✅ | **Кросс-платформенно с первого дня**: Windows + Linux. Целимся на будущий хостинг (Linux VPS). Сборка двух бинарей через CI (Nuitka не кросс-компилируется) |
| ✅ | **`platformdirs`** для путей вместо хардкода `%LOCALAPPDATA%`. Linux → `~/.local/share/fis-monitor/`, Windows → `%LOCALAPPDATA%\fis-monitor\` |
| ✅ | **Автостарт раздельно**: `autostart/windows.py` (Task Scheduler At-Logon) и `autostart/linux.py` (XDG Autostart `~/.config/autostart/`). В MVP реализован Windows, Linux — заглушка |
| ✅ | **Релиз клиенту: только Windows-бинарь**. Linux-бинарь — для разработки и будущего хостинга |

**requirements.txt (черновой состав):**
```
fastapi
uvicorn[standard]
pydantic>=2.0
sse-starlette
selectolax==0.4.8
requests
playwright==1.58.0
psutil          # PID-проверка lock-файла
watchdog        # file-watch на config.json
jinja2          # шаблоны
platformdirs    # кросс-платформенные пути для данных/конфига
```

### UX / Дизайн

Зафиксировано 12.05.2026 после критики UX Researcher. UX-решения реализованы в `claude-design/` (см. [[index]] → «Артефакты от дизайнера»).

| # | Решение |
|---|---|
| ✅ | **Конкурентная срочность как главный UX-приоритет**. Карточка лота показывает возраст крупно и тикает каждую секунду. Цветной левый бордер 4px: алый первые 10 мин, жёлтый до часа, синий до суток, серый дальше. CTA «Открыть на сайте» всегда видна (НЕ hover) |
| ✅ | **Разнесение фильтров**: «Область наблюдения» (макрорегион, fetch-time) — в Настройках. Фильтр уведомлений (subjects РФ) — в Уведомлениях. View-фильтры на главном sidebar — ad-hoc, не сохраняются как настройки |
| ✅ | **Никогда не показывать пустую ленту**. Всегда последние 50 лотов из БД + виджет «Здоровье мониторинга» (последний цикл / всего в базе / последний новый) |
| ✅ | **Разделитель «N новых с последнего визита»** в ленте + summary-тост при возврате после долгого отсутствия |
| ✅ | **Onboarding 4 шага** при первом запуске: (1) область наблюдения (макрорегионы) → (2) SMTP бот-ящика (отправитель), обязательная проверка подключения → (3) email получателя (один или несколько), тестовое письмо → (4) готово (резюме + кнопка «Открыть панель»). Флаг `onboarded=true` в БД |
| ✅ | **ЕСИА: предупреждение за 10 мин до истечения** + модалка перелогина показывается **поверх ленты, но не блокирует чтение**. Notification о появлении модалки в email и браузер |
| ✅ | **User-state на карточке лота**: `user_status` (none/submitted), `user_note` (текст), `user_starred` (bool). Хранятся в user-state, не теряются при reparse |
| ✅ | **Diff-уведомления** (Свободен → Зарезервирован, изменение площади) — отдельный канал, другой звук («pluck» вместо «pop»), серый стиль карточки, без CTA «Открыть» |
| ✅ | **Mobile out of scope MVP**. Минимум 1366×768. Никакого PWA, service-worker, manifest.json. Никакого гамбургер-меню |
| ✅ | **Упрощение контролов**: пагинация 50 (не infinite scroll), только Esc/Enter shortcuts (без J/K/G), тёмная тема только через `prefers-color-scheme` (без тоггла), координаты только десятичные (ДМС в tooltip) |
| ✅ | **Базовый шрифт 16px**, настраиваемый 14/16/18 в Настройках. Контраст всех цветов ≥ 4.5:1 (WCAG AA) |
| ✅ | **`aria-live="polite"`** для ленты лотов (screen-reader озвучивает новые), `assertive` для критичных (сессия истекла, ошибка) |
| ✅ | **Одно уведомление на новый лот, попавший под notify-фильтр** (subjects РФ из настроек). Лоты вне фильтра — тихо появляются в ленте без звука. **Diff-событие (лот ушёл) — БЕЗ звука вообще**, только серая карточка в ленте. **Сервер решает** tier и кладёт в SSE-фрагмент атрибут `data-tier="match\|silent\|gone"`, JS играет звук только для `match` |
| ✅ | **Эскалация звука** при отсутствии реакции: 0с тихий pop → 60с погромче → 120с пульсирующий title + favicon |
| ✅ | **«Не беспокоить до HH:MM»** в шапке с пресетами (1 час / 3 часа / до утра / своё) |
| ✅ | **Catch-up при простое**: если приложение было выключено ≥1 час, при следующем запуске единичный email/баннер «За время простоя появилось N лотов» |
| ✅ | **Onboarding state и user-state — в user-state таблицах**, mirror не трогают (см. `db/schema.sql`) |

### Ответы дизайнеру (12.05.2026)

| # | Решение |
|---|---|
| ✅ | **SMTP-пароль хранится plain в `state.db`**. ПК клиента считается доверенной средой (1 пользователь, локальный комп, файловый ACL на `%LOCALAPPDATA%`). Без шифрования, без keyring — security theater при нашей threat model |
| ✅ | **«Проверить SMTP» в онбординге обязательно**. Кнопка «Далее» в step 2 заблокирована до `✓ подключено`. Исключение: «Пропустить email» → email-канал выключен, идём дальше. При выборе встроенного бот-ящика — тест запускается автоматически при попытке «Далее» |
| ✅ | **Tier лота решает сервер**. При отправке SSE-фрагмента `lot.new` в HTML кладётся `data-tier="match\|silent\|gone"`. JS в `playNotificationSound()` читает атрибут и выбирает звук (или молчание) |

### Removal-detection

| # | Решение |
|---|---|
| ✅ | **Removal — без уведомлений**. Лот, который пропал/изменил статус, появляется в ленте как серая карточка (diff-карточки в `claude-design/templates/`), но не пушится в email/браузер. Это «упустил/поздно» — звуковая срочность не нужна |
| ✅ | **L1 — Passive disappearance**: раз в сутки фул-скан списка для каждого региона. Обновляем `last_seen_at` всех видимых лотов. Лот не виден ≥2 цикла подряд → кандидат на removed. Низкий приоритет, в часы низкой нагрузки сайта |
| ✅ | **L2 — Active verification**: для кандидатов **starred OR submitted OR <7 дней** дёргаем `/cabinet/free-lot-view?id=N`. HTTP 200 + статус ≠ «Свободен» → `status_changed`. 404/302 → `hard_removed`. Параллелизм ≤5 |
| ✅ | **Soft-mark, никогда DELETE**. User-state переживает (отдельная таблица `lot_user_state`) |
| ✅ | **Защита от ложных removed при техработах**: 5xx на списке → НЕ обновляем `last_seen_at` в этом цикле. 5xx на L2 → ретрай до 3 раз, потом `permanent_fail` и ручная отметка. Сайт-донор в техработах не должен «протухнуть» все 400 лотов разом |
| ⚠️ | **5 вопросов для живой проверки** (см. ниже) — до старта L2 в проде |

#### Открытые вопросы — требуют живой проверки с актуальной сессией

1. Реакция `/cabinet/free-lot-view?id=999999` (заведомо несуществующий) — HTTP-код и тело
2. Возможные значения «Статус» в селекте `freelotsearch-status` живого DOM
3. Содержит ли `/cabinet/free-lot` лоты со статусом ≠ «Свободен» если убрать дефолтный фильтр через `FreeLotSearch[freeLotStatus]=…`
4. Реальная карточка известного «ушедшего» лота — клиент должен знать кадастр, по которому раньше можно было подать заявку
5. Работает ли `X-PJAX: true` для фул-скана (может быть в 4-5× дешевле)

До получения ответов на 1-4 — L2 active verification работает, но логика «hard_removed vs status_changed» может потребовать тюнинга.

### Тесты

| # | Решение |
|---|---|
| ✅ | Фикстуры HTML от 12.05.2026 сохранены в `tests/fixtures/` |
| ✅ | Unit-тесты парсера на фикстурах |
| ✅ | Регрессия при апгрейде парсера: прогон всех фикстур, точное совпадение output |

См. также: [[product/mvp-scope]] (финальный скоуп).

---

## См. также

- [[architecture]] — slug-MOC слоёв и швов
- [[onboarding]] — детали FSM ([[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]])
- [[notifications]] — плагин-архитектура каналов
- [[data-model/lot]], [[data-model/notifications]], [[data-model/settings]], [[data-model/sse]], [[data-model/errors]]
