# API Reference

Локальный HTTP API приложения. Bind: `127.0.0.1:8080`.
Аутентификация: CSRF (см. [[decisions-log]] → CSRF).
Все изменяющие запросы требуют `X-CSRF-Token` header + проверку `Origin`/`Host`.

Секреты (пароли, токены) во всех GET-ответах маскируются `***`.

## Лоты

### GET /api/lots
Список лотов с фильтрацией.
**Query**: `region` (int, опц), `limit` (int, дефолт 50), `offset` (int, дефолт 0).
**Response 200**: `{ "lots": [...], "total": N }`.

### GET /api/lots/{id}
Полная карточка лота, включая `raw_json` enrichment-данных.
**Response 200**: объект лота.
**Response 404**: лот не найден.

### GET /api/lots/new
Список новых необработанных лотов (`notified=false`).
**Response 200**: массив лотов.

### GET /api/lots/history
История всех обнаружений за период.
**Query**: `limit`, `offset`.

### POST /api/lots/mark-seen
Пометить лот как просмотренный. CSRF обязательно.
**Body**: `{ "lot_id": N }`.

## Статус и мониторинг

### GET /api/status
Текущее состояние приложения.
**Response 200**: `{ "session": "active|expired", "last_cycle": "...", "queue_size": N, "uptime_sec": ..., "lots_count": N }`.

### GET /api/cycles
История циклов мониторинга (таблица `cycles`).
**Query**: `limit` (дефолт 50), `offset`.
**Response 200**: `{ "cycles": [{ "id", "started_at", "finished_at", "status", "lots_fetched", "new_lots", "error" }, ...] }`.

### POST /api/cycle/run
Триггерит цикл мониторинга вручную. CSRF обязательно.
**Response 202**: `{ "queued": true }`.
Эквивалент `POST /api/check-now` из ранних версий доков.

### POST /api/pause
Поставить цикл мониторинга на паузу. CSRF обязательно.

### POST /api/resume
Возобновить цикл мониторинга. CSRF обязательно.

## Конфиг

### GET /api/config
Текущий `config.json` (без секретов — пароли маскируются `***`).
Схема ключей: см. [[config-reference]].

### PUT /api/config
Обновить config целиком или частично. CSRF обязательно.
**Body**: см. [[config-reference]].
File-watch перезагрузит без рестарта.
**Response 200**: новый config (маскированный).
**Response 422**: ошибка валидации Pydantic — `{ "field": "...", "message": "..." }`.

## Аутентификация ЕСИА

### POST /api/login
Запускает Playwright headed-окно для логина через ЕСИА. CSRF обязательно.
**Response 202**: `{ "started": true }`.
Эквивалент `POST /api/login/start`.

### GET /api/login/status
Текущий статус процесса логина (в процессе / успех / ошибка).
**Response 200**: `{ "state": "idle|running|ok|failed", "message": "..." }`.

### POST /api/logout
Очищает `profile/` (Playwright persistent context). CSRF обязательно.
**Response 200**: `{ "ok": true }`.

## Уведомления

### GET /api/notifiers
Список зарегистрированных плагинов-каналов.
**Response 200**: `[{ "channel_id", "display_name", "description", "enabled", "config_schema", "recipient_label", "recipient_placeholder" }, ...]`.
Конфиг каждого канала с маскированными секретами.

### PUT /api/notifiers/{channel_id}
Обновить конфиг канала и список получателей. CSRF обязательно.
**Body**: соответствует `config_schema` канала.
Пустое поле пароля = «не менять текущее значение».

### POST /api/notifiers/{channel_id}/test
Отправить тестовое уведомление через канал. CSRF обязательно.
**Response 200**: `{ "ok": bool, "message": str }`.

### POST /api/notifiers/{channel_id}/discover-recipients
Авто-обнаружение получателей (например, Telegram `chat_id` через `getUpdates`). CSRF обязательно.
*(планируется в v2, плагин-интерфейс готов в MVP)*

### GET /api/notifications/history
Журнал отправленных уведомлений.
**Query**: `lot_id` (опц), `channel` (опц), `limit`, `offset`.
**Response 200**: `{ "items": [{ "lot_id", "channel", "recipient", "sent_at" }, ...] }`.

## Enrichment

### GET /api/enrichment/queue
Размер очереди enrichment + список ожидающих `lot_id`.
**Response 200**: `{ "size": N, "pending": [lot_id, ...] }`.

### POST /api/enrichment/retry
Перезапустить зависшие задачи enrichment. CSRF обязательно.
**Response 202**: `{ "requeued": N }`.

## Self-diagnostic

### GET /api/export/diagnostic
ZIP с логами + `state.db` (без секретов — пароли SMTP/токены обнуляются).
Для отправки разработчику при проблеме.
**Response 200**: `application/zip`.

## SSE Events

### GET /api/stream
Long-lived SSE stream для live-обновлений UI. Реализация — `sse-starlette` (см. [[decisions-log]]).
Origin-check как у CSRF, токен не требуется (GET, idempotent).
Альтернативный путь `GET /events` — алиас (см. [[web/ui-architecture]]).

#### Event: `lot.new`
Новый лот появился в реестре.
Payload — HTML-фрагмент `_lot_poster.html.jinja` или `_lot_list.html.jinja` (выбирается сервером по контексту вьюхи).
Атрибуты карточки: `data-tier="match|silent|gone"`, `data-lot-id`, `data-age-seconds`.
Frontend играет звук только при `data-tier="match"` (см. [[decisions-log]] → «Tier лота решает сервер»).

#### Event: `lot.status`
Лот изменил статус или пропал (removal-detection).
Payload — HTML-фрагмент с `hx-swap-oob` для in-place замены конкретной карточки.
Атрибут `data-tier="gone"`. **Звук не играется** (см. [[decisions-log]] → Removal-detection).

#### Event: `status`
Статус системы: ЕСИА-сессия, следующий цикл, DND, pause.
Payload — HTML-фрагмент `_header_status.html.jinja`.

#### Event: `expired`
ЕСИА-сессия истекла.
Payload — пустой div с `hx-swap-oob`, снимающий `hidden` с `#session-expired-modal`.

#### Multi-tab fan-out
Один источник (background queue) → N очередей подписчиков. Каждая открытая вкладка — своя очередь.
Реализация: `queue.Queue` per subscriber, fan-out в источнике через broadcast. Sync→async мост:
`await loop.run_in_executor(None, q.get)` в SSE-generator'е (см. [[decisions-log]] → «SSE мост sync→async»,
`web/sse.py`).

#### Origin-check
`GET /api/stream` проверяет `Origin` header: должен быть `http://127.0.0.1:8080` или
`http://localhost:8080`. Иначе `403`.

## Onboarding

Wizard первого запуска (см. [[decisions-log]] → «Onboarding 4 шага», «Onboarding-gate: redirect»).
Пока `state.onboarded != true` middleware редиректит любой GET на `/onboarding?step=1`.

### GET /onboarding?step=N
Шаг wizard'а (1..4). Возвращает `onboarding/wizard.html.jinja` с подгруженным фрагментом шага.

### POST /onboarding/save?step=N
Сохранить шаг. CSRF обязательно. Body — поля step'а (form-encoded).
**Response**: `302` на `?step=N+1` или на `/` после финала (выставляется `state.onboarded=true`).

### POST /onboarding/smtp-test
Проверка SMTP-подключения (используется на шаге 2). CSRF обязательно.
**Body**: `{ "smtp_host", "smtp_port", "smtp_user", "smtp_password", "use_default" }`.
**Response 200**: HTML-фрагмент чипа результата (`✓ подключено` / `✗ ошибка: ...`).
До получения `ok` кнопка «Далее» на шаге 2 заблокирована (исключение — «Пропустить email»,
см. [[decisions-log]] → «Проверить SMTP в онбординге обязательно»).

## См. также

- [[web/ui-architecture]] — обоснование стека и маршрутов
- [[product/monitoring-plan]] — цикл, очереди, безопасность
- [[notifications]] — плагин-архитектура каналов
- [[config-reference]] — таблица ключей `config.json`
- [[decisions-log]] — CSRF, bind, idempotency и прочие решения
