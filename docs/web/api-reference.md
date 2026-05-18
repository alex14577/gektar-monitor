# API Reference

Локальный HTTP API приложения. Bind: `127.0.0.1:8080` (см. [[decisions/ADR-011-dns-rebinding-host-allowlist|ADR-011]]).

**CSRF:** все POST-запросы защищены `CsrfHostOriginMiddleware`. Заголовок `Origin` или `Host` проверяется автоматически — дополнительная обработка в роутах не нужна.

**Onboarding gate:** пока `OnboardingState != COMPLETED`, `OnboardingGateMiddleware` перенаправляет любой не-whitelisted GET на `/onboarding/`. Whitelist: `/static/`, `/events`, `/onboarding/`, `/auth/`.

---

## Auth (`/auth`)

### POST /auth/start

Запустить headed-login (Playwright) через ЕСИА.

**Rate limit:** 1 запрос / 60 с на client IP.
**Single-flight:** второй вызов при уже запущенном job → 409.

**Response:**

| Код | Тело | Условие |
|-----|------|---------|
| 202 | `{"status": "started"}` | Job запущен |
| 409 | `{"detail": "Login already in progress"}` | Job уже работает |
| 429 | `{"detail": "Too many requests — try again in 60 seconds"}` | Rate limit превышен |
| 503 | `{"detail": "Login service not initialized — startup not yet complete"}` | Executor ещё не привязан (фаза 1.5 lifespan не завершена) |

---

### GET /auth/status

Текущий статус login job.

**Response 200:**

```json
{
  "running": true,
  "last_outcome": {
    "success": true,
    "cookies_updated": true,
    "error": null
  }
}
```

`last_outcome` равен `null` если ни одного job ещё не запускалось. Поле `error` — контролируемое enum-значение (`"timeout"`, `"cancelled"`, `"playwright_other"`, `"playwright_missing_binary"`, `"playwright_missing_deps"` и пр.) — никогда не содержит сырые исключения.

---

### POST /auth/cancel

Отменить активный login job. Идемпотентен: возвращает 204 даже если job не активен.

**Response 204** (пустое тело).

---

## Lots (`/lots`)

### GET /lots

Отфильтрованный список лотов с cursor-пагинацией.

**Query-параметры:**

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `regions` | `list[int]` (повторяемый) | — | Фильтр по ID региона (можно передать несколько) |
| `area_sqm_min` | `decimal` | — | Минимальная площадь, кв. м |
| `area_sqm_max` | `decimal` | — | Максимальная площадь, кв. м |
| `status` | `str` | — | Статус лота |
| `cursor` | `str` | — | Opaque cursor для следующей страницы (base64url) |
| `page_size` | `int` | `50` | Размер страницы, от 1 до 200 |

**Response 200:**

```json
{
  "items": [{ ...LotUserDTO... }],
  "next_cursor": "base64url-string-or-null",
  "has_more": true
}
```

**Ошибки:**

| Код | Условие |
|-----|---------|
| 422 | Невалидные параметры фильтрации или некорректный cursor |

---

### GET /lots/{lot_id}

Карточка одного лота по числовому ID.

**Path-параметр:** `lot_id` — целое число.

**Response 200:** объект `LotUserDTO` (JSON).

**Response 404:** `{"detail": "Lot {lot_id} not found"}`.

---

## Notifications (`/notifications`)

### GET /notifications

Список последних записей об отправленных уведомлениях.

**Query-параметры:**

| Параметр | Тип | По умолчанию | Диапазон |
|----------|-----|-------------|---------|
| `limit` | `int` | `100` | 1–500 |

**Response 200:** JSON-массив объектов `NotificationRecord` (сериализован через `model_dump(mode="json")`).

---

## Settings (`/settings`)

### GET /settings

Текущий снимок `Settings`. Поля `SecretStr` (пароли) сериализуются Pydantic как `"**********"` — никогда не попадают в ответ открытым текстом.

**Response 200:** JSON-объект `Settings` (`model_dump(mode="json")`).

---

### POST /settings/smtp

Сохранить SMTP-credentials. Трёхфазная валидация:
1. Формат (Pydantic `SmtpCredentials`).
2. DNS + host policy (`SmtpHostPolicy.resolve_and_check`) — **вне транзакции**.
3. Запись в `state.db` через `SmtpCredentialsRepository.save()`.

**Request body (`SmtpCredentialsBody`):**

| Поле | Тип | По умолчанию | Описание |
|------|-----|-------------|----------|
| `smtp_user` | `str` | обязательно | Логин SMTP |
| `smtp_password` | `str` | обязательно | Пароль SMTP |
| `smtp_host` | `str` | обязательно | SMTP-сервер |
| `smtp_port` | `int` | `587` | Порт |
| `use_default` | `bool` | `true` | Использовать дефолтный from-адрес |

**Response:**

| Код | Условие |
|-----|---------|
| 204 | Успех, тело пустое |
| 400 | DNS / host policy ошибка (`SmtpHostPolicyError`) |
| 422 | Невалидное имя хоста, порт вне диапазона, пустые обязательные поля |

---

### POST /settings/smtp/test

Отправить тестовое письмо. Использует синтетический фикстурный `LotPublicDTO` (id=0, cadastral_no=`"00:00:0000000:0000"`) — полный SMTP-путь (STARTTLS, DNS, аутентификация) проверяется реально.

**Request body (`SmtpTestBody`):**

| Поле | Тип | Описание |
|------|-----|----------|
| `recipient` | `str` | Email-адрес получателя тестового письма |

**Response 200:**

```json
{"ok": true, "detail": "..."}
```

`ok` равен `false` при ошибке отправки; `detail` содержит описание без PII.

---

### POST /settings/regions

Заменить список отслеживаемых регионов. Триггерит watchdog-reload настроек.

**Request body (`RegionsBody`):**

| Поле | Тип | Ограничения |
|------|-----|-------------|
| `regions` | `list[int]` | min_length=1; каждый элемент ge=1, le=80 |

**Response 200:** `{"ok": true}`.

**Response 422:** пустой список, значение вне 1–80.

---

### POST /settings/recipients

Заменить список email-получателей уведомлений. Пустой список допустим — отключает email-уведомления.

**Request body (`RecipientsBody`):**

| Поле | Тип | Ограничения |
|------|-----|-------------|
| `recipients` | `list[EmailStr]` | по умолчанию `[]`; каждый элемент — валидный RFC-5321 email |

**Response 200:** `{"ok": true}`.

**Response 422:** невалидный email-адрес.

---

## Diagnostics (`/diagnostics`)

### POST /diagnostics/build

Собрать диагностический ZIP-архив (`diagnostic.zip`) с `state.db`, `audit.jsonl` и логами.

**Fail-closed:** если обнаружен schema-drift (лишняя колонка или отсутствующая таблица в `state.db`), возвращается 503 с общим сообщением — детали схемы не раскрываются (ADR-012, R3-M5, R4-M10). PII-поля исключаются через `DiagnosticsExcludePolicy`. `audit.jsonl` исключается из архива если `data_dir` находится в облачном хранилище (Dropbox, iCloud и др.).

**Response:**

| Код | Тип контента | Условие |
|-----|-------------|---------|
| 200 | `application/zip`, filename=`diagnostic.zip` | Успех |
| 500 | `application/json` | Сборка архива не удалась по другой причине |
| 503 | `application/json`, `{"error": "schema_drift", "ui_message": "..."}` | Schema-drift (fail-closed) |

---

## Events (`/events`)

### GET /events

SSE-стрим live-обновлений для браузера. Media type: `text/event-stream`.

**Origin check (DNS-rebinding защита):** GET-запросы не проходят через `CsrfHostOriginMiddleware` (safe method), поэтому роут проверяет заголовок `Origin` самостоятельно:
- Нет `Origin` — разрешено.
- `Origin` в whitelist — разрешено.
- `Origin` не в whitelist — **421 Misdirected Request**.

**Response:** `StreamingResponse` с заголовками `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

**SSE-события:**

| Тип события | Описание | Payload |
|-------------|----------|---------|
| `lot.new` | Новый лот в реестре | HTML-фрагмент карточки лота (`data-tier`, `data-lot-id`, `data-age-seconds`) |
| `lot.status` | Лот изменил статус или пропал | HTML-фрагмент с `hx-swap-oob` |
| `status` | Состояние системы (сессия, следующий цикл, DND) | HTML-фрагмент `_header_status.html.jinja` |
| `session.expired` | ЕСИА-сессия истекла | HTML-фрагмент с `hx-swap-oob`, снимающий `hidden` с `#session-expired-modal` |
| `login.succeeded` | Headed-login завершён успехом; UI сбрасывает stale auth-chip и cycle-error | HTML-фрагмент с `hx-swap-oob` для `#cycle-result` (очищает ошибку предыдущего цикла) и `#session-expired-modal` (скрывает модалку) |

Неизвестные типы событий молча дропаются внутри `SseStreamer` (schema-drift protection, `sse.schema_drift` в лог).

---

## Onboarding (`/onboarding`)

Wizard первого запуска. FSM: `not_started → regions_set → smtp_configured → recipients_set → completed`. Полная спецификация — [[onboarding]].

### GET /onboarding/state

Текущее состояние FSM и URL для отображения соответствующего шага.

**Response 200:**

```json
{"state": "not_started", "url": "/onboarding/regions"}
```

Возможные значения `state`: `not_started`, `regions_set`, `smtp_configured`, `recipients_set`, `completed`.

---

### POST /onboarding/advance

Попытка перехода между состояниями FSM.

**Request body (`AdvanceBody`):**

| Поле | Тип | Описание |
|------|-----|----------|
| `from_state` | `str` | Ожидаемое текущее состояние (строковое значение enum) |
| `to_state` | `str` | Целевое состояние |

**Response:**

| Код | Условие |
|-----|---------|
| 204 | Переход выполнен, тело пустое |
| 409 | Переход запрещён: guard не пройден или mismatch состояния. Тело: `{"error": "invalid_transition", "current_state": "...", "redirect_to": "/onboarding"}` |
| 422 | `from_state` или `to_state` — невалидное значение enum |

---

### POST /onboarding/skip-email

Установить флаг `email_skipped`. Разрешено только в состояниях `smtp_configured` или `recipients_set`.

**Response:**

| Код | Условие |
|-----|---------|
| 204 | Флаг установлен, тело пустое |
| 409 | Текущее состояние не позволяет skip. Тело: `{"error": "invalid_transition", "current_state": "...", "redirect_to": "/onboarding"}` |

---

### Wizard UI (HTML, not in OpenAPI schema)

Следующие маршруты возвращают HTML и помечены `include_in_schema=False`. Они используются браузером и htmx, а не REST-клиентами.

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/onboarding` | Bare entry: 302 на `url_for_current_step()` |
| GET | `/onboarding/regions` | Шаг 1 (state=`not_started`). Несовпадение состояния → 302 |
| GET | `/onboarding/smtp` | Шаг 2 (state=`regions_set`). Несовпадение → 302 |
| GET | `/onboarding/recipients` | Шаг 3 (state=`smtp_configured`). Несовпадение → 302 |
| GET | `/onboarding/test-email` | Шаг 4 (state=`recipients_set`). Несовпадение → 302 |
| POST | `/onboarding/save?step=N` | htmx dispatcher: form-data по шагу, action=`next`\|`skip`. Успех → 200 + `HX-Redirect`. Ошибка валидации → 200 + re-render фрагмента |
| POST | `/onboarding/smtp-test` | htmx фрагмент: проверка SMTP. Form-data: `smtp_host`, `smtp_port`, `smtp_login`, `smtp_pass`. Ответ — `<span id="smtp-test-result" ...>` для htmx outerHTML swap. Всегда 200 |

---

## Main (`/`)

### GET /

HTML главного экрана (feed). Доступен только при `OnboardingState=COMPLETED` (gate middleware гарантирует это до роута). Шаблон `feed.html.jinja`.

**Помечен** `include_in_schema=False`. Контекст: `SessionStatus` (через `SessionProbe`), `Settings.interval_minutes`, активные лоты через `LotQueryService`. Cookie `view_filters` читается через `ViewFiltersService.deserialize` и конвертируется в `LotFilters.subject_display_names` через `build_feed_context` (в `web/feed_context.py`). Рендеринг `#feed` происходит через `build_feed_context` (shared helper с `POST /filters/view`). Шаблон подключается к SSE-каналам `/sse/lots` и `/sse/status` самостоятельно через htmx-sse.

---

### POST /filters/view

Обновить ленту лотов при изменении фильтров (htmx endpoint).

**Помечен** `include_in_schema=False`.

**Request:** `application/x-www-form-urlencoded`, form-data поля:

| Поле | Тип | Допустимые значения | По умолчанию |
|------|-----|---------------------|--------------|
| `subjects` | `str[]` | site-id субъекта (строка) | `[]` |
| `area_min` | `str \| null` | целое ≥ 0 или пусто | `null` |
| `area_max` | `str \| null` | целое ≥ 0 или пусто | `null` |
| `only_new` | `str \| null` | присутствие ключа = `true` | `false` |
| `only_stars` | `str \| null` | присутствие ключа = `true` | `false` |
| `sort_dir` | `str \| null` | `"desc"` \| `"asc"` | `"desc"` |

`sort_dir` управляет порядком сортировки лотов в фиде (DESC = новее первым, ASC = старше первым). Невалидное или отсутствующее значение тихо приводится к `"desc"` (silent default, не 422). Значение сохраняется в cookie `view_filters` и переживает F5.

**Response:**

| Код | Content-Type | Тело | Дополнительно |
|-----|-------------|------|---------------|
| 200 | `text/html` | rendered `<div id="feed">` + OOB-блок `<div id="filter-trigger" hx-swap-oob="true">` | `Set-Cookie: view_filters=<serialized>; Path=/; HttpOnly; SameSite=Lax` |

**htmx contract:**
- `hx-target="#feed"`, `hx-swap="outerHTML"` — заменяет `#feed` основным телом ответа.
- OOB-блок `id="filter-trigger"` обновляет кнопку-триггер в сайдбаре (вне `#feed`), используя htmx out-of-band swap (см. [[glossary#htmx OOB swap]]).

**Cookie persistence:** сохранённые фильтры читаются `GET /` при следующей загрузке страницы через `ViewFiltersService.deserialize` → `build_feed_context` (в `web/feed_context.py`) → `LotFilters.subject_display_names`.

**Empty-state:** «Ничего не подходит» рендерится ВНУТРИ `<div id="feed">` (не снаружи) — иначе outerHTML-swap его не задевает.

---

## См. также

- [[web/ui-architecture]] — обоснование стека и маршрутов
- [[onboarding]] — спецификация FSM и контракт 409-тела
- [[decisions/ADR-011-dns-rebinding-host-allowlist|ADR-011]] — CSRF, bind, Origin-check
- [[decisions/ADR-015-smtp-host-validation|ADR-015]] — DNS-вне-tx инвариант для SMTP
- [[decisions/ADR-018-onboarding-fsm-server-enforced|ADR-018]] — Onboarding gate
- [[decisions/ADR-019-notification-state-machine|ADR-019]] — Notification state machine
- [[decisions/ADR-025-sse-single-endpoint|ADR-025]] — единый роут `/events`
