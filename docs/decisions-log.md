# Журнал решений

Все зафиксированные решения после ревью архитектуры. Дата фиксации: 12.05.2026.

## Оглавление

### Решения по доменам (раннее обсуждение)

- [Что в MVP](#что-в-mvp)
- [Парсинг и сервер](#парсинг-и-сервер)
- [Уведомления](#уведомления)
- [Безопасность и операции](#безопасность-и-операции)
- [Хранение](#хранение)
- [Логи и наблюдаемость](#логи-и-наблюдаемость)
- [Forward-compat с хостингом](#forward-compat-с-хостингом)
- [Технологический стек](#технологический-стек)
- [UX / Дизайн](#ux--дизайн)
- [Ответы дизайнеру (12.05.2026)](#ответы-дизайнеру-12052026)
- [Removal-detection](#removal-detection)
- [Тесты](#тесты)

### ADR-блоки (зафиксированы после 5 раундов архитектурного ревью)

**Структура и сборка:**
- [ADR-001: Notifier — Protocol, не ABC](#adr-001-notifier--protocol-не-abc)
- [ADR-002: Plugin discovery — explicit registry](#adr-002-plugin-discovery--explicit-registry-не-entry_points)
- [ADR-004: Composition root — самописный Container, разделённый на Infra/Services](#adr-004-composition-root--самописный-container-разделённый-на-infraservices)
- [ADR-006: import-linter в CI](#adr-006-import-linter-в-ci)

**Конкурентность, lifespan, БД:**
- [ADR-005: Concurrency — soft-yield, retry SQLITE_BUSY, без unified writer-queue](#adr-005-concurrency--soft-yield-retry-sqlite_busy-без-unified-writer-queue)
- [ADR-007: Per-connection PRAGMA vs persistent](#adr-007-per-connection-pragma-vs-persistent)
- [ADR-014: Two-phase shutdown policy](#adr-014-two-phase-shutdown-policy)
- [ADR-016: Repository invariants — BEGIN IMMEDIATE + identifier whitelist + private _sync_geo](#adr-016-repository-invariants--begin-immediate--identifier-whitelist--private-_sync_geo)

**Уведомления и события:**
- [ADR-003: Error strategy — Exception для всего, Result только для Notifier](#adr-003-error-strategy--exception-для-всего-result-только-для-notifier)
- [ADR-008: EventBus — двухконтурный (normal/critical), без persistence в БД](#adr-008-eventbus--двухконтурный-normalcritical-без-persistence-в-бд)
- [ADR-019: Notification state machine — reserve → attempt → sent | permanent_fail](#adr-019-notification-state-machine--reserve--attempt--sent--permanent_fail)

**Безопасность:**
- [ADR-010: Data_dir location policy](#adr-010-data_dir-location-policy)
- [ADR-011: DNS-rebinding защита — strict Host allow-list](#adr-011-dns-rebinding-защита--strict-host-allow-list)
- [ADR-012: Diagnostic.zip — explicit allow-list + redactor](#adr-012-diagnosticzip--explicit-allow-list--redactor)
- [ADR-013: Locker — OS-level lock, PID info-only](#adr-013-locker--os-level-lock-pid-info-only)
- [ADR-015: SMTP host validation — IP/DNS rules + resolve-recheck](#adr-015-smtp-host-validation--ipdns-rules--resolve-recheck)
- [ADR-017: Secrets handling — SecretStr + crash-dump exclusion](#adr-017-secrets-handling--secretstr--crash-dump-exclusion)
- [ADR-018: Onboarding FSM server-enforced](#adr-018-onboarding-fsm-server-enforced)
- [ADR-020: SMTP host/port SSOT = state.db](#adr-020-smtp-hostport-ssot--statedb-r4-c1)
- [ADR-021: Manual STARTTLS — обход smtplib server_hostname bug](#adr-021-manual-starttls--обход-smtplib-server_hostname-bug-при-connect-by-ip-r4-c2)
- [ADR-022: ALLOWED_TRACKED_FIELDS SSOT + SmtpHostPolicyError наследует UpstreamError](#adr-022-allowed_tracked_fields-ssot-через-typingget_args--smtphostpolicyerror-наследует-upstreamerror)

**Резервирование:**
- [ADR-009: Backup стратегия — только USER_STATE_TABLES](#adr-009-backup-стратегия--только-user_state_tables)

---

## Что в MVP

| # | Решение |
|---|---|
| ✅ | **Каталог НЕ в MVP** (v2). Архитектурно заложить расширяемость на каталог в схеме БД и API |
| ✅ | **Telegram НЕ в MVP** (v2 через панель плагинов). Архитектурно заложить плагин-систему |
| ✅ | **Heartbeat-сводка опционально** — чекбокс в настройках, по умолчанию выключено |
| ✅ | **Auto-update полностью убран**. Никакой подписи манифеста, GitHub Releases, ничего. Обновления — архивом руками |
| ✅ | **Lazy enrichment** при первом запуске — качаем только первую страницу, дальше обычным циклом |

## Парсинг и сервер

| # | Решение |
|---|---|
| ✅ | `per-page=10`, окончательно везде в коде и docs |
| ✅ | Сортировка `sort=-DATE_CREATE`, ранний выход на знакомом id |
| ✅ | Интервал между циклами — **настройка пользователя** в панели, диапазон **0..60 минут**, дефолт 15. Значение `0` = непрерывный режим, следующий цикл стартует сразу после завершения предыдущего |
| ✅ | **Фильтры — только 2**: `regions` (макрорегион 1=ДФО, 2=Арктика) и `rf_subjects` (субъекты РФ). Площадь, категория, ВРИ — не фильтруем |
| ✅ | **Семантика фильтров**: в БД храним **все лоты** с выбранных макрорегионов (`regions` — fetch-time, ограничено URL сайта). Фильтр `rf_subjects` применяется **только к уведомлениям** (notify-time), на отображение в UI можно навешивать любые ad-hoc фильтры поверх. Прошлое не теряем при изменении фильтра |
| ✅ | Час пояс хранения: **МСК**, опция в админ-панели «изменить TZ отображения» |
| ✅ | **Защита от смены ID-схемы** — проверка max(id_new_page) >= last_known_id между циклами, см. [[monitoring-plan]] |

## Уведомления

| # | Решение |
|---|---|
| ✅ | В MVP — только **браузер** (Notification API + SSE) и **email** |
| ✅ | **Email-источник: наш бот-ящик** + дефолтный SMTP в config. Клиент через панель может **переопределить** SMTP (host/port/login/password) если хочет свой |
| ✅ | Список получателей email — задаётся в панели, можно несколько |
| ✅ | Плагин-архитектура уведомлений — да (для будущего расширения) |

## Безопасность и операции

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

## Хранение

| # | Решение |
|---|---|
| ✅ | Гибрид-схема: колонки + JSON-blob + FTS5 + R-tree (см. [[db/schema|db/schema.sql]]) |
| ✅ | `id INTEGER PRIMARY KEY` = data-key сайта |
| ✅ | `cadastral_no` — НЕ UNIQUE, просто INDEX |
| ✅ | Raw HTML карточек хранить (gzip, ~30 МБ через год) |
| ✅ | История изменений: только `status, area_sqm, date_update, auction` |
| ✅ | Разделить таблицы на **mirror** (можно стереть) и **user-state** (не теряем) |
| ✅ | `parser_version` в каждой строке, lazy reparse при апгрейде |
| ✅ | Шифрование БД НЕ нужно (данные публичные) |

## Логи и наблюдаемость

| # | Решение |
|---|---|
| ✅ | Структурные JSON-логи в файлы (ротация посуточно, 30 дней) |
| ✅ | Таблица `cycles` в БД: `id, started_at, finished_at, status, lots_fetched, new_lots, error` |
| ✅ | Журнал запросов — отдельный файл `requests.jsonl` (не в БД) |
| ✅ | Self-diagnostic export через панель — zip с логами + state.db (без секретов) |

## Forward-compat с хостингом

| # | Решение |
|---|---|
| ✅ | Архитектурно поддерживать переезд на VPS — переменная окружения `MODE=local\|server` |
| ⏳ | Мульти-юзер, изоляция cookies, регистрация оператора ПДн — отдельный проект v3 |

## Технологический стек

Зафиксировано 12.05.2026 после ответов на 6 блокирующих вопросов аудита.

| # | Решение |
|---|---|
| ✅ | **Pydantic v2** — для config.json и NotifierConfig (схемы плагинов) |
| ✅ | **sqlite3 sync** (встроенный) — без `aiosqlite`. Минимум зависимостей |
| ✅ | **requests** (sync) — простой HTTP-клиент. Параллельность enrichment через `ThreadPoolExecutor` (до 10 тредов) |
| ✅ | **selectolax** — HTML-парсер. Верифицирован на фикстурах: парсит таблицу списка (`table.kv-grid-table tbody tr[data-key]`) и детальную карточку (`div.request-domain__key-value`) корректно. Ограничение: нет `:scope`-селектора (обход через `.iter()`) |
| ✅ | **`playwright==1.58.0`** + Chromium 145.0.7632.6 (Chrome for Testing). Релиз 2026-01-30, 3.5 месяца в проде |
| ⚠️ | **Fallback Playwright 1.56.0** — если ЕСИА залогируется и CfT-сборку 1.58 будет flag-ать антифрод. Зафиксировать в [[runbook]] как известную точку отказа |
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

## UX / Дизайн

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
| ✅ | **Onboarding state и user-state — в user-state таблицах**, mirror не трогают (см. [[db/schema|db/schema.sql]]) |

## Ответы дизайнеру (12.05.2026)

| # | Решение |
|---|---|
| ✅ | **SMTP-пароль хранится plain в `state.db`**. ПК клиента считается доверенной средой (1 пользователь, локальный комп, файловый ACL на `%LOCALAPPDATA%`). Без шифрования, без keyring — security theater при нашей threat model |
| ✅ | **«Проверить SMTP» в онбординге обязательно**. Кнопка «Далее» в step 2 заблокирована до `✓ подключено`. Исключение: «Пропустить email» → email-канал выключен, идём дальше. При выборе встроенного бот-ящика — тест запускается автоматически при попытке «Далее» |
| ✅ | **Tier лота решает сервер**. При отправке SSE-фрагмента `lot.new` в HTML кладётся `data-tier="match\|silent\|gone"`. JS в `playNotificationSound()` читает атрибут и выбирает звук (или молчание) |

## Removal-detection

| # | Решение |
|---|---|
| ✅ | **Removal — без уведомлений**. Лот, который пропал/изменил статус, появляется в ленте как серая карточка (diff-карточки в `claude-design/templates/`), но не пушится в email/браузер. Это «упустил/поздно» — звуковая срочность не нужна |
| ✅ | **L1 — Passive disappearance**: раз в сутки фул-скан списка для каждого региона. Обновляем `last_seen_at` всех видимых лотов. Лот не виден ≥2 цикла подряд → кандидат на removed. Низкий приоритет, в часы низкой нагрузки сайта |
| ✅ | **L2 — Active verification**: для кандидатов **starred OR submitted OR <7 дней** дёргаем `/cabinet/free-lot-view?id=N`. HTTP 200 + статус ≠ «Свободен» → `status_changed`. 404/302 → `hard_removed`. Параллелизм ≤5 |
| ✅ | **Soft-mark, никогда DELETE**. User-state переживает (отдельная таблица `lot_user_state`) |
| ✅ | **Защита от ложных removed при техработах**: 5xx на списке → НЕ обновляем `last_seen_at` в этом цикле. 5xx на L2 → ретрай до 3 раз, потом `permanent_fail` и ручная отметка. Сайт-донор в техработах не должен «протухнуть» все 400 лотов разом |
| ⚠️ | **5 вопросов для живой проверки** (см. ниже) — до старта L2 в проде |

### Открытые вопросы — требуют живой проверки с актуальной сессией

1. Реакция `/cabinet/free-lot-view?id=999999` (заведомо несуществующий) — HTTP-код и тело
2. Возможные значения «Статус» в селекте `freelotsearch-status` живого DOM
3. Содержит ли `/cabinet/free-lot` лоты со статусом ≠ «Свободен» если убрать дефолтный фильтр через `FreeLotSearch[freeLotStatus]=…`
4. Реальная карточка известного «ушедшего» лота — клиент должен знать кадастр, по которому раньше можно было подать заявку
5. Работает ли `X-PJAX: true` для фул-скана (может быть в 4-5× дешевле)

До получения ответов на 1-4 — L2 active verification работает, но логика «hard_removed vs status_changed» может потребовать тюнинга.

## Тесты

| # | Решение |
|---|---|
| ✅ | Фикстуры HTML от 12.05.2026 сохранены в `tests/fixtures/` |
| ✅ | Unit-тесты парсера на фикстурах |
| ✅ | Регрессия при апгрейде парсера: прогон всех фикстур, точное совпадение output |

См. также: [[mvp-scope]] (финальный скоуп).

---

## ADR-блоки (зафиксированы после ревью архитектуры)

Раздел дополнен после ревью Code Reviewer / Backend Architect / Security Engineer / Database Optimizer (см. [[architecture]] §0, §11). Каждый блок: context / decision / consequences.

### ADR-001: Notifier — Protocol, не ABC

**Context.** Первая версия `notifications.md` описывала `Notifier` как `ABC` с дефолтным методом `send_to_all` и retry-логикой. Это связывает наследников с реализацией базы (изменение базы ломает наследников) и нарушает «composition over inheritance».

**Decision.** `Notifier` — `typing.Protocol`. Retry — функция-декоратор `with_retry(notifier, attempts, backoff) -> Notifier` (structurally compatible). `send_to_all` — снят с интерфейса, живёт в `NotifierDispatcher` (у него есть доступ к `NotificationsRepository` для idempotency).

**Consequences.** Плюсы: композиция, легче тестировать, легче добавлять каналы (нет требования наследоваться). Минус: дублирование `channel_id`/`display_name` декларации в каждом классе — но это всё равно ClassVar, не overhead.

### ADR-002: Plugin discovery — explicit registry, не entry_points

**Context.** Notifier-каналы — плагины. Варианты discovery: entry_points, auto-discover, explicit registry.

**Decision.** Explicit registry в composition root. **Nuitka onefile** ломает entry_points (требуется `__file__`-обход, в onefile неконсистентен). Supply-chain — entry_points позволяет сторонним пакетам инжектировать notifier без явного согласия. В MVP все каналы — наши.

**Consequences.** Добавление канала = новый класс + 1 строка в `composition.py`. Никакой магии при импорте. При появлении сторонних плагинов (v3+) — миграция на entry_points с fallback.

### ADR-003: Error strategy — Exception для всего, Result только для Notifier

**Context.** Когда использовать Exception vs Result?

**Decision.** Двухконтурно. **Contour 1**: `UpstreamError(category=...)` (network/http_4xx/http_5xx/redirect_login/timeout) и `DomainError` — exception. Поднимаются из адаптеров, ловятся в use case `run_forever()`. **Contour 2**: `NotifyResult(ok, detail, retryable)` — только для `Notifier.send()` и `.test()`. Один канал упал — остальные идут, нужна структура для retry по `retryable`.

**Consequences.** HttpClient — exception (никакого Result). Нотификации — Result, retry на основании `retryable` флага. Python без `?`-оператора слишком шумный для всеобщего Result.

### ADR-004: Composition root — самописный Container, разделённый на Infra/Services

**Context.** Контейнер для ~15 швов. Варианты: `dependency-injector`, `inject`, самописный.

**Decision.** Самописный, ~200 строк, типизирован. Container — НЕ один God-объект; разделён на frozen `Infra` (швы, repos, инфра-адаптеры) и frozen `Services` (use cases). Оба `repr=False` — против утечки secrets в crash-логи.

**Consequences.** Никакой магии, ясные слои сборки (Layer 0..4), порядок зависимостей виден по коду. Минус — больше boilerplate, чем `dependency-injector`. Принимаем.

### ADR-005: Concurrency — soft-yield, retry SQLITE_BUSY, без unified writer-queue

**Context.** Decisions-log упоминал «единую очередь» для приоритезации. Ревью DBA: централизованная queue добавляет много сложности, прибыли мало.

**Decision.** «Единая очередь» из decisions-log трактуется **как SQLite writer-lock на уровне WAL**, не Python writer-thread. Приоритезация реализуется через:
1. `busy_timeout=5000` per-connection.
2. **Retry SQLITE_BUSY с jitter обязателен на ВСЕХ writers** (5 попыток, exponential backoff с jitter).
3. **`cycle_in_progress` — SOFT-YIELD флаг**: enrichment проверяет → `sleep(50ms)`. Это **не mutex**, не блокирует при сбое cycle.
4. Full_scan коммитит батчами по 50 + `sleep(50ms)` между батчами.

**Consequences.** Простая модель, нет priority inversion, нет нового потока-арбитра. Цена — каждый writer должен реализовать retry-обёртку (одна функция-декоратор в `infra/sqlite/`).

### ADR-006: import-linter в CI

**Context.** Слои domain/services/infra/web легко деградируют без автоматической проверки.

**Decision.** Закрепить через `import-linter` в CI. Контракты (R3-M4 — добавлен `composition` layer):
- `domain` ∉ {`sqlite3`, `infra`, `services`, `web`, `composition`, `fastapi`, `requests`}.
- `services` ∉ {`infra`, `web`, `composition`, `fastapi`, `sqlite3`, `requests`}.
- `infra` ∉ {`web`, `composition`}.
- `web` ∉ {`composition`}.
- `composition` (`composition.py`, `app.py`) — разрешён импорт из всех слоёв (это его задача — собирать граф).

Конкретный фрагмент `.importlinter`:
```ini
[importlinter]
root_package = fis_monitor

[importlinter:contract:layers]
name = Layered architecture
type = layers
layers =
    fis_monitor.composition | fis_monitor.app
    fis_monitor.web
    fis_monitor.services
    fis_monitor.infra
    fis_monitor.domain

[importlinter:contract:domain_purity]
name = Domain doesn't touch infrastructure libs
type = forbidden
source_modules = fis_monitor.domain
forbidden_modules = sqlite3, requests, fastapi, playwright, smtplib
```

**Consequences.** +1 dev-зависимость, +`.importlinter` в репо. Гарантия что архитектура не деградирует по мере роста. `composition.py` живёт «над» web (он импортирует роуты в `app.py`), но не наоборот.

> **Note (R5 review — DB)**: `compute_changes` в `domain/diff.py` импортируется из `infra/sqlite/lot_repo.py` — это легально по onion (infra→domain разрешён). Зафиксировать в `.importlinter` config: `layers` с `domain` строго ниже `infra`. CI-проверка через `lint-imports` обязательна.

### ADR-007: Per-connection PRAGMA vs persistent

**Context.** Часть PRAGMA сохраняется в файле БД (`journal_mode`), часть — атрибут коннекта (`busy_timeout`).

**Decision.** **Persistent в `schema.sql`**: `journal_mode=WAL`, `auto_vacuum=INCREMENTAL`, `wal_autocheckpoint`, `user_version`. **Per-connection в `ThreadLocalConnectionProvider._configure`**: `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=OFF`, `temp_store=MEMORY`, `cache_size=-20000`, `mmap_size=268435456`.

**Consequences.** Нет «забытого PRAGMA» после reconnect. `schema.sql` декларативен, `_configure` — единственное место setup-а.

### ADR-008: EventBus — двухконтурный (normal/critical), без persistence в БД

**Context.** SSE-события могут теряться при медленном подписчике. Какие можно дропать, какие — нет?

**Decision.** Два метода: `publish_normal` (drop-from-tail при maxsize=100, для `lot.new` и UI-уведомлений) и `publish_critical` (blocking `put(timeout=2.0)`, для `session.expired`, `cycle.error`, `smtp.failed`). При timeout критичного — force-unsubscribe slow consumer. **Persistence событий в БД — НЕТ** (БД содержит lot/notification как source of truth, F5 восстановит).

**Consequences.** Простая memory-only модель. UX-события могут быть пропущены вкладкой — это OK. Критичные гарантированно доставлены либо подписчик отвалится (что и хотим).

**Расширение R3-C5 (per-type slots + payload whitelist).** Persist last-critical event делится на **per-type ключи** в таблице `state`: `last_critical_event:session`, `last_critical_event:cycle`, `last_critical_event:smtp` (TTL 1ч каждый). Single-slot терял пачку (session.expired в 10:00, cycle.error в 10:30 — клиент при reconnect видел только cycle.error). Persist'имые поля — фильтр через `SsePayloadSchema` (whitelist по типу): для `cycle.error` — `{timestamp, cycle_id, error_category}` БЕЗ stacktrace/exception_repr; для `smtp.failed` — `{timestamp, channel_id, error_category, attempt_no}` БЕЗ recipient/smtp_response. `logger.warning` при force-unsubscribe тоже редактируется по тому же whitelist. Закрывает утечку PII через `last_critical_event:*` (stacktrace в state — это слой PII при экспорте/диагностике, хотя `audit.jsonl` уже исключён — defence-in-depth).

### ADR-009: Backup стратегия — только USER_STATE_TABLES

**Context.** Бэкапить весь `state.db` или только user-state?

**Decision.** Только user-state: `lot_user_state`, `notifications`, `smtp_credentials`, `state`. Алгоритм: новая пустая БД → user-state DDL → `executemany` копирование. Размер ~1 МБ, ротация 7 дней, файлы `userstate-YYYY-MM-DD.sqlite` в `data_dir/backups/`.

**Consequences.** Mirror (lots/lots_history/lot_html_archive/cycles/FTS/R-tree) НЕ бэкапим — восстанавливается прогоном. Бэкап маленький, безопасный (нет PII в mirror), быстрый.

### ADR-010: Data_dir location policy

**Context.** Пользователь может разместить data_dir внутри облачного синка (OneDrive/Dropbox/Yandex/`%USERPROFILE%\Documents`). SQLite-WAL + облачный синк = коррапт БД.

**Decision.** В composition root при инициализации — проверка пути. При совпадении с одним из cloud-sync паттернов: `logger.warning` + UI-баннер «БД находится в облачном хранилище — это может привести к повреждению. Перенесите `data_dir`».

**Consequences.** Установщик по умолчанию использует `%LOCALAPPDATA%` (не Documents) — там нет облачного синка by default. Для пользователей, переехавших на нестандартный путь — явное предупреждение.

**Расширение R3-minor (cloud-sync detection — конкретный список паттернов).** В `warn_if_in_cloud_sync(path)` сначала делается `os.path.realpath(path)` (резолв symlinks/junction points). Substring-match по case-insensitive списку: `OneDrive`, `Dropbox`, `Yandex.Disk`, `YandexDisk`, `Google Drive`, `GoogleDrive`, `iCloudDrive`, `pCloud`, `Mega`, `MEGAsync`, `Resilio`, `Sync.com`, `Box`. Для Windows дополнительно: `%USERPROFILE%\Documents`, `%USERPROFILE%\OneDrive`. Линтером не покрывается — только runtime warning + UI-баннер.

### ADR-011: DNS-rebinding защита — strict Host allow-list

**Context.** Bind на 127.0.0.1 не защищает от DNS-rebinding: вредоносный сайт резолвит `attacker.example` → `127.0.0.1`, далее браузер шлёт POST на наш endpoint с правильным Cookie.

**Decision.** Middleware:
- **Host header**: только `127.0.0.1:8080` или `localhost:8080`. Иное → **421 Misdirected Request**.
- **Origin/Referer**: whitelist `http://127.0.0.1:8080`, `http://localhost:8080`. Иное → 403. НЕ «непустой» — точное совпадение.

**Consequences.** Защита от DNS-rebinding на уровне приложения. EventSource (SSE) всегда шлёт same-origin Origin — не ломается. CSRF + Host allow-list = двойной контур.

### ADR-012: Diagnostic.zip — explicit allow-list + redactor

**Context.** Пользователь шлёт diagnostic.zip разработчику. Не должно протечь ничего секретного.

**Decision.**
- **Allow-list таблиц для экспорта**: `lots`, `cycles`, `notifications(lot_id, channel, sent_at)` (БЕЗ recipient). Таблица `smtp_credentials` физически не открывается (DB cursor не касается).
- **Redactor для логов** на этапе сборки zip (regex на Cookie/Authorization/`?code=`/`?state=`, СНИЛС/паспорт/ИНН/email).
- **MANIFEST.txt** в zip — список включённого + app-version.

**Consequences.** Безопасный экспорт. Цена — отдельный `DiagnosticsService` (~150 строк) + конфигурируемые redactor-regex.

**Расширение второго раунда (audit.jsonl isolation).** Полные значения config-diff (включая `smtp.host`, `recipients[]`, `interval_minutes`) пишутся ТОЛЬКО в append-only `audit.jsonl` в `data_dir/`. Этот файл **физически исключён** из DiagnosticsService allow-list (наряду с `smtp_credentials`). В `app.jsonl` идут только счётчики и булы (см. §7.6 architecture.md, N-M5). Так PII не утекает в диагностический архив, отправляемый разработчику.

### ADR-013: Locker — OS-level lock, PID info-only

**Context.** Single-instance lock через PID-файл уязвим к race condition (PID может быть переиспользован).

**Decision.** Локer ОБЯЗАН использовать OS-level lock: `fcntl.flock(LOCK_EX|LOCK_NB)` на Linux, `msvcrt.locking` на Windows. Файл открывается с `O_NOFOLLOW|O_EXCL`. PID записывается в файл только для info («кто держит лок»).

**Consequences.** Корректная single-instance без race. PID-info полезен для диагностики, не для арбитража.

### ADR-014: Two-phase shutdown policy

**Context.** `supervisor.shutdown(timeout=10)` против HTTP timeout=30s + SMTP send=30s → каждая остановка фиксировала WARN с pending threads. Network timeouts арифметически больше supervisor-deadline.

**Decision.** Двухфазный shutdown:
- **Phase 1 (graceful, `grace_timeout=35s`)**: `stop_event.set()` + join каждого потока. 35с = `max(network_timeouts) + 5s` запас. Каждый `run_forever(stop_event)` проверяет event между итерациями/батчами/fetch'ами.
- **Phase 2 (forceful)**: при истечении grace — WARN с pending thread-stacks (через `faulthandler.dump_traceback`); `executor.shutdown(wait=False, cancel_futures=True)`; dangling threads помечены `daemon=True` при start (Python прибьёт при interpreter exit).
- **Network timeouts ≤ grace_timeout - 5s — обязательный инвариант**: HTTP `timeout=(10, 25)` (connect, read), SMTP connect=10s + send=20s + close=5s. Playwright nav=20s, action=10s.
- `conn_provider.close_all()` — ТОЛЬКО после phase 2 (иначе writers упадут с SQLITE_MISUSE).

**Consequences.** Shutdown без warn-флопа при гладком закрытии запросов. Цена: документированный инвариант на каждый network adapter, проверяется в config (lint/test). Расширяет ADR-005.

**Расширение R3-C3 (Playwright headed-login — pw_executor special-case).** `pw_executor` исключён из phase 1 supervisor.shutdown (Playwright sync API в C-extension не реагирует на `stop_event`; `cancel_futures=True` отменяет только pending). Добавлена **phase 1.5** между phase 1 и phase 2: `LoginService.cancel_active_job()` зовёт `LoginSession.cancel()`, который делает `browser.close()` извне worker-thread — активный `page.wait_for_url` развернётся с `TargetClosedError` за ~2-3 секунды, job завершится с `LoginOutcome(success=False, error="cancelled")`. После — `pw_executor.shutdown(wait=True)`. Дополнительно: `open_headed_login(deadline=300.0)` — hard timeout 5 минут (страховка от пользователя, закрывшего вкладку без логина). UI показывает «Закройте окно браузера для остановки» если headed-login активен при shutdown.

**Расширение R3-M3 (Known limitations).**
- **Windows shutdown машины**: `WaitToKillAppTimeout` по умолчанию 5с — phase 1 grace=35с не успевает; in-flight notifications/lots могут не записаться. Документировано в [[runbook]]: «при shutdown машины монитор не гарантирует доставку in-flight уведомлений». Будущее улучшение (не MVP): `SetConsoleCtrlHandler(CTRL_SHUTDOWN_EVENT)` fast-path с grace_timeout=4с.
- **systemd**: для корректного shutdown unit-файл должен иметь `TimeoutStopSec=45s` (grace 35с + phase 1.5 + 2 запас). Указано в installer-скрипте и runbook.
- **macOS (если когда-то)**: launchd по умолчанию даёт 5с — тот же класс проблемы что Windows.

Принимаем как known-limitation: machine-shutdown — не предмет гарантий MVP. Graceful app-shutdown (через UI / Ctrl+C) — гарантируется.

### ADR-015: SMTP host validation — IP/DNS rules + resolve-recheck

**Context.** Первая версия `SmtpCredentials.host` validator имела дыры: IPv4-mapped IPv6, IPv6 unique-local `fc00::/7`, link-local `fe80::/10`, multicast, cloud-metadata `169.254.169.254`, IPv4-compatible `::a.b.c.d`, `0.0.0.0`/`::`. Также TOCTOU между Pydantic-валидацией и реальным `smtplib.SMTP(host)` — DNS может резолвится в RFC1918 после save.

**Decision.** Разделение domain vs infra:
- **`SmtpCredentials` (domain)** — Pydantic-модель с чистым формат-валидатором (syntactically valid IP/hostname, длина, отсутствие CR/LF).
- **`SmtpHostPolicy` (infra)** — `infra/smtp/host_policy.py`. Универсальное правило через `ipaddress.ip_address(resolved).{is_private|is_loopback|is_link_local|is_multicast|is_reserved|is_unspecified}` + IPv4-mapped IPv6 распаковка + отдельное правило для cloud-metadata + TLD-blocklist (`*.lan`, `*.local`, `*.internal`, `*.corp`, `*.home`, `*.localdomain`, `*.test`, `*.example`, `*.invalid`, `*.localhost`).
- **DNS resolve recheck** через `socket.getaddrinfo(host, port)` — проверка ВСЕХ A/AAAA. Применяется в двух точках: `SettingsService.set_smtp_credentials()` (на save) и `SmtpEmailNotifier.send()` ПЕРЕД connect (на каждый отправку — закрывает TOCTOU).

**Consequences.** Закрывает SSRF-вектор. Цена: каждая отправка email делает дополнительный getaddrinfo (~ms). Domain не знает про infra-policy — корректное разделение.

**Расширение R3-C4 (connect-by-IP + SNI verify).** `SmtpHostPolicy.check()` deprecated в пользу `resolve_and_check(host, port) -> ResolvedSmtpEndpoint`. Без этого оставался TOCTOU: policy делала `getaddrinfo` → проверяла → возвращала None; `smtplib.SMTP(host).connect()` делал **повторный** `getaddrinfo`, и атакующий с DNS-MITM мог вернуть RFC1918 IP между двумя resolve-ами. Теперь `SmtpEmailNotifier.send()` использует `endpoint.ip` для connect (pin'нутый IP), `endpoint.original_host` для `EHLO` и SNI. TLS-cert validation идёт по original hostname через `ssl.create_default_context()` (check_hostname=True) и `starttls(context=ctx)` — smtplib передаёт original_host как `server_hostname` для SNI. Connect-by-IP не ломает TLS — это стандартный паттерн `connect(ip) + verify(hostname)`. `ResolvedSmtpEndpoint` — infra-dataclass, см. data-model.md.

### ADR-016: Repository invariants — BEGIN IMMEDIATE + identifier whitelist + private _sync_geo

**Context.** Первая версия `LotRepository.upsert(lot, *, tracked_fields)` нарушала SRP: репозиторий вычислял diff (нормализация status casing, datetime-precision) — это бизнес-правило, не CRUD. Параметр `tracked_fields: Sequence[str]` потенциально — vector identifier-инъекции (имя поля в SQL не параметризуется). `sync_geo` был публичным методом Protocol → утечка инварианта «вызывать только внутри upsert-tx».

**Decision.**
1. **Вариант A: caller считает diff** (см. §3.1). `LotRepository.upsert(lot, *, changes: list[FieldChange])` — repo принимает уже готовый список. `compute_changes()` и `normalize_for_diff()` — чистые функции в `domain/diff.py`.
2. **`BEGIN IMMEDIATE`** — обязательный для всех read-then-write (`upsert`, `mark_inactive`, `set_last_known_id`). Захватывает writer-lock до первого SELECT.
3. **Identifier whitelist**: `FieldChange.field: Literal[...]` ограничивает на типе; `ALLOWED_TRACKED_FIELDS` frozenset — defence-in-depth runtime check.
4. **`_sync_geo` приватный**: из публичного Protocol убран, зовётся только внутри `upsert`. Будущий публичный `update_geo` — отдельный метод с BEGIN IMMEDIATE.

**Consequences.** Repo стал тонким CRUD + tx-invariant. Diff-политика тестируется без БД. Identifier-инъекции исключены на типах. R-tree consistency гарантирована (нет внешнего пути менять lat/lon без _sync_geo).

**Расширение R3-C2 (`compute_changes` зовётся repo внутри tx).** Caller-stage `get(id)` + `compute_changes(old, new)` + `upsert(new, changes=changes)` имел silent data-corruption window: между `get` (no-tx) и `upsert` (BEGIN IMMEDIATE) другой writer мог изменить ту же строку — `upsert` писал в `lots_history` фантомный `old_value`. Решение: caller передаёт **только** `tracked: Sequence[TrackedField]`; repo внутри своей BEGIN IMMEDIATE tx делает `SELECT old`, импортирует чистую domain-функцию `compute_changes`, вычисляет diff, пишет историю. `compute_changes` остаётся в `domain/diff.py` (testable in-memory без БД). Импорт infra→domain валиден (DIP — domain независим от infra; infra легально использует domain как библиотеку). Двойной SELECT устранён (`was_new` возвращается в `LotUpsertResult`). SRP: domain — «как считать diff»; infra — «выполнить diff атомарно в tx и записать историю»; caller — «дать новый лот».

**Расширение R3-M8 (`_sync_geo` для всех переходов lat/lon).** `_sync_geo` зовётся при любом изменении координат, включая `value→NULL` (DELETE FROM lots_rtree) и `NULL→value` (INSERT). Если оба NULL — `DELETE FROM lots_rtree WHERE id=?`. Integration-тест покрывает: `NULL→value`, `value→NULL`, `value→value'`, no-change (R-tree row не трогается).

### ADR-017: Secrets handling — SecretStr + crash-dump exclusion

**Context.** `SmtpCredentials.password: str` риск утечки через `__repr__` в crash-логах. Diagnostic.zip мог зацепить `*.dmp`/`core.*`/`Werfault*`/`CrashDumps/` с фрагментами адресного пространства.

**Decision.**
- `SmtpCredentials.password: pydantic.SecretStr`. `__repr__`/`__str__` → `'***'`. Получить plain — только через `.get_secret_value()`.
- `DiagnosticsService` exclude-list расширен: `*.dmp`, `core.*`, `Werfault*`, `CrashDumps/`. Дополняет ADR-012.

**Consequences.** Двойной контур защиты secrets (логи + crash-dumps). Никакого overhead в runtime (SecretStr — wrapper).

### ADR-018: Onboarding FSM server-enforced

**Context.** Первая версия onboarding-gate редиректила на `?step=N` из query-param. Пользователь мог `GET /?step=4` и пропустить настройку SMTP.

**Decision.** Server-side state-machine с явными states, transitions, guards. `OnboardingService.advance(from_state, to_state)` валидирует переход. Middleware редиректит на **последний валидный step** (читая из БД), не на query-param. Полная спецификация — `docs/onboarding.md`.

States: `not_started → regions_set → smtp_configured → recipients_set → completed`. Guards включают `len(regions) > 0`, `smtp_test.last_result.ok OR email_skipped`, `len(recipients) > 0 OR email_skipped`, `test_email_sent OR email_skipped`.

**Consequences.** Невозможно пропустить шаг. Цена: state в БД (key `onboarding_state`), `OnboardingService.advance()` — атомарная операция через BEGIN IMMEDIATE. UI читает текущий state и редерит соответствующий step.

**Known limitation R3-M10 (`smtp_test_last_result_ok` подделывается через direct DB-write).** В trust-model MVP (single-user, доверенная локальная среда, ACL на `%LOCALAPPDATA%`) — приемлемо. Future hardening (не MVP): `HMAC(state_secret, host+port+user+timestamp)` записывается рядом с флагом, OnboardingService.advance проверяет HMAC. Roadmap-TODO.

### ADR-019: Notification state machine — reserve → attempt → sent | permanent_fail

**Context.** `notifications.md` декларировал `Dispatcher.mark_attempt(...)` (write-ahead перед `send`), но в `schema.sql` таблица `notifications` имела PK `(lot_id, channel, recipient)` + `sent_at NOT NULL DEFAULT CURRENT_TIMESTAMP`. Не было колонки `attempt_no`, не было pending-состояния. Контракт `NotificationsRepository.mark_attempt(..., attempt_no: int)` без поля в БД = silent no-op либо runtime-ошибка. На рестарте Dispatcher не знал, продолжать ли retry или начинать с нуля.

**Decision.** Notifications — state machine с тремя состояниями (`pending`, `sent`, `permanent_fail`) и одним PK `(lot_id, channel, recipient)` (одна запись на адресата — idempotency сохраняется).

Изменения схемы (`schema.sql`):
- `status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (...))` — состояние.
- `attempt_no INTEGER NOT NULL DEFAULT 0` — счётчик попыток, durable.
- `last_attempt_at TIMESTAMP` — nullable до первой попытки.
- `sent_at TIMESTAMP` — стал nullable (NULL пока status != 'sent').
- Partial-индекс `idx_notifications_pending` на `last_attempt_at WHERE status='pending'` — для recovery после рестарта.

Контракт `NotificationsRepository` (см. notifications.md):
- `reserve(lot_id, channel, recipient) -> bool` — INSERT OR IGNORE + status='pending', attempt_no=0.
- `mark_attempt(lot_id, channel, recipient, at) -> int` — UPDATE attempt_no=attempt_no+1, last_attempt_at=at WHERE status='pending', RETURNING attempt_no.
- `mark_sent(lot_id, channel, recipient, at)` — UPDATE status='sent', sent_at=at WHERE status='pending'.
- `mark_permanent_fail(lot_id, channel, recipient)` — UPDATE status='permanent_fail' WHERE status='pending'.
- `status_of(...)` — для skip уже отправленных.
- `list_pending_older_than(age)` — recovery на старте consumer-loop.

Каждый метод — короткая отдельная tx (BEGIN IMMEDIATE). Сетевой `send()` идёт **между** `mark_attempt` и `mark_sent` — открытую writer-tx на десятки секунд держать недопустимо. Цена: между mark_attempt и mark_sent возможен рестарт; recovery подхватит status='pending' и повторит — attempt_no уже инкрементирован, idempotency на адресата сохраняется (PK + проверка status_of).

**Consequences.** Полный durable state machine. Рестарт во время retry — продолжается с того же attempt_no. Идемпотентность на адресата гарантируется PK + проверкой `status_of` перед reserve. Backoff корректно работает поверх рестартов. Цена: 3 новых колонки в `notifications` (~ничего по размеру). `already_sent()` deprecated в пользу `status_of() == 'sent'`.

**Расширение R4-C5 (at-least-once семантика + Message-ID дедупликация).** PK + `status_of()` защищают **только запись в БД** (idempotency на уровне нашей таблицы). Они НЕ защищают от дубликата на стороне MTA: при крэше процесса между «SMTP 250 OK от сервера» и `mark_sent` COMMIT запись остаётся `status='pending'`, recovery (`list_pending_older_than`) повторит → второе письмо уйдёт. Окно дубля — секунды (crash-window между ACK и COMMIT). Утверждать «exactly-once» некорректно — это **at-least-once на адресата**.

Митигация: детерминированный `Message-ID: <{lot_id}.{channel}.{sha256(recipient)[:16]}@fis-monitor.local>` (RFC 5322 §3.6.4). Major MTA (Gmail, Yandex, Mail.ru, Outlook) дедуплицируют по Message-ID на стороне получателя — повторное письмо с тем же ID отбрасывается. `recipient` хешируется чтобы не светить email в логах MTA-цепочки (Received-headers могут публично попасть в bounces).

Не блокер для MVP single-user. Известно и документировано в runbook (см. сценарий 11 / при жалобе на дубль).

**Расширение R4-C3 (recovery zombie-резерватов с `last_attempt_at IS NULL`).** `list_pending_older_than(age)` ВКЛЮЧАЕТ записи где `last_attempt_at IS NULL` (zombie — created `reserve()` но процесс крэшнулся до первого `mark_attempt`). SQL:
```sql
SELECT ... FROM notifications
 WHERE status='pending'
   AND (last_attempt_at IS NULL OR last_attempt_at < :cutoff);
```
Без `OR ... IS NULL` zombie-резерват вечно болтался бы pending, не виден recovery. Индекс `idx_notifications_pending` хранит NULL last_attempt_at (partial WHERE status='pending') — обе ветки SQL'я индексны.

**Расширение R4-C4 (`mark_attempt -> int | None` race).** `mark_attempt(lot_id, channel, recipient)` возвращает `int | None`. None — если запись уже `sent` либо `permanent_fail` (race с конкурентным consumer / recovery / cap_reached в R4-M6). Caller (`_send_one`) обязан пропустить отправку:
```python
attempt_no = self.notif_repo.mark_attempt(lot_id, ch, recipient)
if attempt_no is None:
    return  # race — уже в финальном статусе
```
Без этого race на reserve → mark_attempt → permanent_fail (например, конкурентный cap_reached) бы приводил к UnboundLocalError либо raise. Race — легитимный путь, не баг.

**Расширение R4-M6 (hard-cap на общее число попыток).** `MAX_TOTAL_ATTEMPTS = 10`. После N рестартов с recovery (`attempt_no > MAX_TOTAL_ATTEMPTS`) — `mark_permanent_fail`, лог `notification.cap_reached`. Защита от бесконечного retry при перманентной невозможности отправки (e.g. provider sustained outage 24h+).

**Расширение R4-M8 (migration v1→v2 для notifications + smtp_credentials).** `PRAGMA user_version` bumped 1→2. MigrationRunner v1→v2 — следующий SQL (запускается в одной BEGIN IMMEDIATE tx):

> **FIXME (R5 review)**: SQL ниже физически невыполним в SQLite — `ALTER TABLE` не может ослабить `NOT NULL` constraint на `sent_at` без rebuild table. Greenfield MVP создаёт БД сразу с `user_version=2`, поэтому migration v1→v2 в проде не выполнится — runtime impact нулевой. При реальной миграции (v2→v3 в будущем) переписать через 12-step rebuild pattern из SQLite docs: CREATE TABLE _new + INSERT SELECT + DROP old + RENAME.

```sql
BEGIN IMMEDIATE;

-- notifications: state machine (ADR-019)
ALTER TABLE notifications ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'
    CHECK (status IN ('pending','sent','permanent_fail'));
ALTER TABLE notifications ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1;
ALTER TABLE notifications ADD COLUMN last_attempt_at TIMESTAMP;
-- Существующие строки v1 имели sent_at NOT NULL — это всё «успешные» отправки,
-- DEFAULT 'sent' их так и помечает. last_attempt_at нет в v1 → ставим sent_at:
UPDATE notifications SET last_attempt_at = sent_at WHERE status='sent';
-- sent_at в v2 nullable (для future pending records) — старые данные не меняются.

-- smtp_credentials: SSOT host/port (ADR-020)
ALTER TABLE smtp_credentials ADD COLUMN smtp_host TEXT NOT NULL DEFAULT 'smtp.yandex.ru';
ALTER TABLE smtp_credentials ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587
    CHECK (smtp_port BETWEEN 1 AND 65535);
-- Defaults — литералы бот-ящика. После migration MigrationRunner может опционально
-- ALTER COLUMN убрать DEFAULT (SQLite не поддерживает DROP DEFAULT на ALTER —
-- придётся через rebuild table; в greenfield MVP это no-op).

-- Indexes (R4-M9 + R4-C3)
DROP INDEX IF EXISTS idx_notifications_sent_at;   -- старая версия без partial
CREATE INDEX IF NOT EXISTS idx_notifications_sent_at
    ON notifications(sent_at DESC) WHERE status='sent';
CREATE INDEX IF NOT EXISTS idx_notifications_pending
    ON notifications(last_attempt_at) WHERE status='pending';

PRAGMA user_version = 2;
COMMIT;
```

Greenfield MVP не имеет prod-баз с v1 — реальные пользователи получат сразу v2 при первом запуске installer'а через `schema.sql`. MigrationRunner есть для совместимости с unit-тестами и dev-данными, и для будущих v2→v3.

### ADR-020: SMTP host/port SSOT = state.db (R4-C1)

**Context.** Первая версия notifications.md/data-model.md упоминала `smtp_host`/`smtp_port` в `EmailConfig` (config.json), а `smtp_user`/`smtp_password` — в state.db (`smtp_credentials`). Это создавало:
1. **Split SSOT** — два места хранения одного логического объекта (creds = host+port+user+password). Pydantic-валидация config.json и UPSERT в state.db — две разные tx, нет атомарности. Race window между UI «сохранить SMTP-настройки» и `SmtpEmailNotifier.send()`: новый host может подгрузиться раньше нового user → попытка login против чужого хоста.
2. **Config-write-vector** — `config.json` имеет write-API через `WatchdogConfigSource` reload (модификация файла на диске → reload). Атакующий с write-доступом к config.json (например, через misconfigured ACL) мог бы перенаправить SMTP на свой хост, не трогая `smtp_credentials` — далее жертва шлёт письма с своими creds на attacker.example.
3. **R4-C1 — schema.sql::smtp_credentials НЕ содержал smtp_host/smtp_port** — `SqliteSmtpCredentialsRepository.save()` физически некуда было писать эти поля. Блокер для кода.

**Decision.** **SSOT = state.db**. Расширить `smtp_credentials`:
```sql
ALTER TABLE smtp_credentials ADD COLUMN smtp_host TEXT NOT NULL;
ALTER TABLE smtp_credentials ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587
    CHECK (smtp_port BETWEEN 1 AND 65535);
```
`Pydantic SmtpCredentials` в domain получает поля `smtp_host: str` и `smtp_port: int`.

В `EmailConfig` (`config.json`) остаётся **только** `use_default_smtp: bool` (формальный признак). Литералы `smtp.yandex.ru:587` хранятся в коде (`infra/smtp/defaults.py`) — fallback при пустой таблице первой установки. Поля `smtp_host`/`smtp_port` в EmailConfig — **deprecated**, читаются только для миграции при первом запуске v2.

`SettingsService.set_smtp_credentials(creds)` пишет ВСЕ 4 поля (host, port, user, password) в одну BEGIN IMMEDIATE tx — атомарность гарантируется.

**Consequences.**
- Когезия: один логический объект — одна таблица — одна tx.
- Защита от config-write-vector: SMTP-host нельзя подменить через config.json без write-доступа к state.db (ACL `%LOCALAPPDATA%`).
- Pydantic-модель Settings БОЛЬШЕ НЕ содержит smtp-секретов и smtp-host/port → diagnostic.zip exclude-list упрощается (state.db.smtp_credentials и так не открывается, см. ADR-012).
- Цена: bump `user_version` 1→2 + migration script (R4-M8). Greenfield MVP не имеет prod-баз с v1, но MigrationRunner и ADR должны быть готовы.

### ADR-021: Manual STARTTLS — обход smtplib server_hostname bug при connect-by-IP (R4-C2)

**Context.** ADR-015 ext (R3-C4) утверждал что `smtp.starttls(context=ctx)` корректно работает с connect-by-IP: «smtplib передаёт original_host как server_hostname для SNI». Security Engineer (4-й раунд) показал, что это **неверно** — реальный CPython source:

```python
# smtplib.SMTP.starttls(context):
self.sock = context.wrap_socket(self.sock, server_hostname=self._host)
#                                                          ^^^^^^^^^^
```
`self._host` устанавливается в конструкторе `SMTP(host=...)`. Поскольку мы зовём `SMTP(host=endpoint.ip)` (для pin'нутого connect), `server_hostname=ip_literal` — TLS-cert verify валится против IP (cert hostname = `smtp.yandex.ru`, presented host = `87.250.250.X`):
- **Availability-bug**: `ssl.SSLCertVerificationError: Hostname mismatch` → email не отправляется.
- **Если бы мы выключили `check_hostname` для воркэраунда — security-bug**: MITM прозрачен, любой TLS-cert на любой host принимается.

**Decision.** Manual STARTTLS — обходим `smtp.starttls()` и вручную `ssl.wrap_socket` с правильным `server_hostname`:

```python
endpoint = self.host_policy.resolve_and_check(creds.smtp_host, creds.smtp_port)
smtp = smtplib.SMTP(host=endpoint.ip, port=endpoint.port, timeout=connect_timeout)
smtp.ehlo(endpoint.original_host)

if creds.use_starttls:
    code, _ = smtp.docmd("STARTTLS")
    if code != 220:
        raise SmtpStarttlsError(code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    smtp.sock = ctx.wrap_socket(smtp.sock,
                                server_hostname=endpoint.original_host)
    smtp.file = None
    smtp.ehlo(endpoint.original_host)   # повторный EHLO после TLS

smtp.login(creds.smtp_user, creds.smtp_password.get_secret_value())
smtp.sendmail(from_addr, [recipient], msg_bytes)
smtp.quit()
```

Альтернативы рассмотрены и отвергнуты:
- `SMTP(host=endpoint.original_host)` + override `socket.getaddrinfo` через monkeypatch на endpoint.ip — глобальный side-effect.
- `SMTP_SSL` (implicit TLS, 465 port) — Yandex поддерживает, но major bot-аккаунт настроен на 587 STARTTLS. Не меняем UX «port 587 default».
- Дождаться CPython фикса (вероятно `smtplib.SMTP(host_for_sni=...)`) — версии Python 3.12..3.14 баг присутствует.

**Consequences.** TLS-cert verification работает корректно (cert против `smtp.yandex.ru`, connect по pin'нутому IP). MITM/DNS-rebinding закрыт. Цена: ~15 строк boilerplate вместо одного `smtp.starttls()`. Документировать в `SmtpEmailNotifier` docstring почему руками.

---

### ADR-022: ALLOWED_TRACKED_FIELDS SSOT через typing.get_args + SmtpHostPolicyError наследует UpstreamError

**Context.** Два смежных кодовых решения из `domain/diff.py` и `domain/errors.py`, не покрытые явным ADR.

**Decision 1 — ALLOWED_TRACKED_FIELDS SSOT.**
`ALLOWED_TRACKED_FIELDS: frozenset[str] = frozenset(typing.get_args(TrackedField))` вместо ручного дублирования значений Literal. Альтернатива — держать два отдельных определения (Literal для типов, frozenset для runtime-проверки) — риск дрейфа при добавлении нового tracked-поля: тип обновлён, frozenset забыт → инъекция в SQL-identifier остаётся незакрытой.

**Decision 2 — SmtpHostPolicyError(UpstreamError).**
Ошибки DNS-resolve и blocklist при проверке SMTP-хоста классифицируются как `upstream` (ADR-003: UpstreamError — для network/DNS/HTTP-слоя). Альтернатива — отдельная иерархия от `DomainError` — создаёт неоднородность: `SmtpEmailNotifier.send()` ловит и `UpstreamError`, и `SmtpHostPolicyError` разными `except`-ветками, нарушая принцип «один обработчик на тип сбоя».

**Consequences.** Нулевая возможность дрейфа между Literal и runtime-frozenset. `SmtpHostPolicyError` обрабатывается в `run_forever()` единым `except UpstreamError` блоком без дополнительных ветвлений.

---

См. также: [[architecture]] (§0, §11), [[onboarding]] (детали FSM).
