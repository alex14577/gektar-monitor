# Config Reference

Файл `config.json` в `%LOCALAPPDATA%\fis-monitor\`.
Изменения подхватываются через file-watch без рестарта (см. [[decisions-log]]).
Полный пример: [[monitoring-plan]] → раздел «Конфиг».

## Таблица ключей

| Путь | Тип | Дефолт | Валидация | Меняется в UI |
|---|---|---|---|---|
| `mode` | enum | `"local"` | `local` \| `server` | нет (env `MODE`) |
| `interval_minutes` | int | `15` | `0..60` (0 = непрерывно, без паузы между циклами) | да |
| `timezone` | str | `"Europe/Moscow"` | IANA TZ | да |
| `regions` | int[] | `[1, 2]` | min 1 элемент. Макрорегионы: 1=ДФО, 2=Арктика. **Fetch-time** — определяет какие данные тянутся с сайта и попадают в БД | да |
| `filters.rf_subjects` | int[] | `[]` | пусто = все субъекты выбранных макрорегионов. **Notify-time** — фильтрует уведомления, в БД хранятся все лоты выбранных макрорегионов | да |
| `monitoring.full_scan_time` | str | `"04:00"` | `HH:MM`, локальное время. Расписание ежедневного full_scan для removal-detection (L1) | да |
| `monitoring.full_scan_l2_priority_days` | int | `7` | ≥0. L2 active verification вне starred/submitted дёргается для лотов младше N дней | да |
| `ui.bind_host` | str | `"127.0.0.1"` | IP | нет |
| `ui.port` | int | `8080` | `1024..65535` | нет |
| `ui.auto_open_browser` | bool | `true` | — | да |
| `ui.font_size_px` | int | `16` | `14` \| `16` \| `18` | да |
| `ui.theme` | enum | `"auto"` | `auto` \| `light` \| `dark` (в MVP `auto` через `prefers-color-scheme`, см. [[decisions-log]]) | да |
| `notifications.email.enabled` | bool | `true` | — | да |
| `notifications.email.use_default_smtp` | bool | `true` | — | да |
| `notifications.email.smtp_host` | str | `"smtp.yandex.ru"` | hostname | да (override) |
| `notifications.email.smtp_port` | int | `587` | `1..65535` | да |
| `notifications.email.from_address` | str\|null | `null` | email \| null | да |
| `notifications.email.recipients` | str[] | `[]` | каждый — email | да |
| `notifications.browser.enabled` | bool | `true` | — | да |
| `notifications.heartbeat.enabled` | bool | `false` | — | да |
| `notifications.heartbeat.time` | str | `"09:00"` | `HH:MM` | да |
| `notifications.sound_escalation.enabled` | bool | `true` | — | да |
| `notifications.sound_escalation.escalate_at_seconds` | int[] | `[60, 120]` | каждый ≥0, возрастающая последовательность | да |
| `notifications.dnd.until` | str\|null | `null` | ISO timestamp \| `null` (выключено). Устанавливается из шапки UI пресетами 1ч/3ч/до утра/своё | да |
| `notifications.catchup.enabled` | bool | `true` | — | да |
| `notifications.catchup.min_offline_minutes` | int | `60` | ≥0. Порог простоя для catch-up-уведомления при возврате | да |

**SMTP-логин и пароль** (`smtp_user`, `smtp_password`) хранятся в `state.db`
(таблица `smtp_credentials`), **не в `config.json`**. См. [[decisions-log]] → «SMTP-пароль
хранится в state.db» и [[data-model]] → `SmtpCredentials`. Pydantic-схема `config.json`
этих полей не содержит. Изменяются через `PUT /api/notifiers/email`
(см. [[api-reference]]).

## Секреты

В `config.json` секретов нет (SMTP-пароль вынесен в `state.db`, см. выше).
Общая политика обращения с секретами:
- В ответе `GET /api/config` и `GET /api/notifiers` пароли маскируются `***`.
- В `PUT /api/notifiers/email` пустое значение пароля = «не менять текущее».
- При экспорте через `GET /api/export/diagnostic` — обнуляются (см. [[api-reference]]).
- На диске `state.db` лежит под ACL `%LOCALAPPDATA%` — достаточно для нашей threat model
  ([[notifications]] → «Хранение секретов», [[decisions-log]] → «SMTP-пароль plain в state.db»).

## Валидация

При старте приложения config валидируется через **Pydantic v2** (см. [[decisions-log]]).
Невалидный config → приложение **НЕ стартует**, окно «Не удалось загрузить конфиг» с указанием поля и причины.

Изменения через `PUT /api/config`:
- Принимаются только целиком валидные config'и (атомарно).
- При ошибке валидации — `422` с `{ "field": "...", "message": "..." }`.
- File-watch применяет изменения без рестарта.

## Forward-compat

В режиме `MODE=server` (env, отдельный проект v3, см. [[decisions-log]] → forward-compat):
- `ui.bind_host` → `0.0.0.0`
- Добавляются `auth.*` ключи (multi-user, изоляция cookies per `user_id`)
- Cookies-store изолируется per `user_id`
- Playwright крутится в отдельном контейнере на стороне сервера

Сейчас архитектурно поддерживаем, не реализуем.

## См. также

- [[monitoring-plan]] — полный пример `config.json` и структура папки
- [[decisions-log]] — обоснование решений (Pydantic v2, file-watch, секреты)
- [[notifications]] — детали каналов и хранения секретов
- [[api-reference]] — эндпоинты `GET/PUT /api/config`
