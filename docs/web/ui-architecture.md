# Архитектура: веб-UI (главное решение проекта)

Решено заменить десктоп-стек (winotify + pystray + Windows toast) на **локальный веб-сервер с UI в браузере**.

## Почему

1. **Кросс-платформенность бесплатно** — работает на Windows, macOS, Linux без отдельного кода.
2. **Разработка из Linux без Windows VM** — критический путь больше не требует виртуалки.
3. **UI значительно богаче** — таблицы, фильтры, история, графики вместо тостов.
4. **Forward-compatibility с хостингом** — тот же код можно запустить на VPS, превратив в SaaS. Локальная версия для пользователя сейчас, серверная — для масштабирования потом.
5. **Дизайн делается отдельно** — пользователь готовится отдельно, не блокирует разработку бэкенда.
6. **Расширяемость через панель** — Telegram-токен, webhook'и, любые ключи и настройки пользователь сможет ввести **сам в форме на странице**, без правки JSON и без перезапуска. Это особенно ценно для интеграций, которые добавляются после установки.

## Схема

```
┌────────────────────────────────────────┐
│   Браузер пользователя (Chrome/Edge)        │
│   http://localhost:8080                │
│                                        │
│   ▸ Список новых лотов                 │
│   ▸ История за неделю                  │
│   ▸ Фильтры (регионы, площадь)         │
│   ▸ Статус последней проверки          │
│   ▸ Кнопка "Перелогиниться"            │
│   🔔 Push-уведомления через            │
│      Notification API                  │
└─────────────┬──────────────────────────┘
              │ HTTP + SSE (live-обновления)
              ▼
┌────────────────────────────────────────┐
│   Локальный Python-бэкенд              │
│   FastAPI + uvicorn на localhost:8080  │
│                                        │
│   - крутит цикл мониторинга            │
│   - отдаёт API + HTML                  │
│   - SQLite                             │
│   - запускает Playwright для логина    │
│     по кнопке из UI                    │
└────────────────────────────────────────┘
```

## Стек

| Слой | Выбор | Почему |
|---|---|---|
| Backend | **FastAPI + uvicorn** | async из коробки, SSE проще чем во Flask, типизация |
| Frontend | **HTMX + Jinja2** | без npm/webpack/node, серверный рендеринг, минимум JS |
| (опция) | Vue/Alpine.js | если дизайнер захочет реактивно |
| Real-time | **SSE (Server-Sent Events)** | проще WebSocket, идеален для push-уведомлений |
| Уведомления в браузере | **Notification API** (W3C) | стандарт, работает во всех современных браузерах |
| Опционально v2 | **PWA** (manifest.json + service-worker.js) | уведомления при закрытой странице |
| Browser-логин | **Playwright Chromium** (отдельный процесс) | как и было — кнопка в UI запускает окно |
| Хранилище | **SQLite + WAL** | без изменений |
| Упаковка | **Nuitka onefile** | то же, что было |

## Модель уведомлений

### MVP — live-страница + Notification API
- Пользователь держит вкладку `localhost:8080` открытой (или ярлык на рабочем столе)
- Новые лоты прилетают через SSE → карточка появляется сверху таблицы + звук + меняется заголовок вкладки
- При наличии разрешения — браузер показывает системное уведомление через **Notification API** (это, по сути, тот же Windows toast, но запрашивается у браузера)

### v2 — PWA для фоновых уведомлений
- `manifest.json` делает страницу «приложением»: пользователь жмёт «Установить», на рабочем столе появляется иконка, запускается в отдельном окне
- `service-worker.js` работает в фоне браузера, шлёт ОС-уведомления даже когда вкладка закрыта (пока работает любой процесс браузера в системе)

### Фолбэк (убран)
PowerShell-фолбэк **убран** (см. [[decisions-log]]). В MVP уведомления — только браузер (Notification API + SSE) и email. Если пользователь закроет браузер полностью, дублирующий канал — email на список получателей из панели.

## Расширение интеграций через UI

Web-панель сильно упрощает подключение **любых внешних сервисов** без перевыпуска и переустановки:

- **Telegram-бот** (изначально не входил в MVP): пользователь в разделе «Уведомления» вводит токен бота и chat_id → бэкенд сохраняет в config → следующий новый лот летит и в браузер, и в Telegram. Без правки файлов, без перезапуска приложения.
- **Email/SMTP**: тот же подход — форма со SMTP-настройками.
- **Webhook на CRM**: поле «URL для webhook» в настройках → каждый новый лот шлётся POST'ом в `amocrm/bitrix/n8n`.
- **Любая будущая интеграция** — добавляется как ещё одна вкладка настроек.

Это и есть главное прикладное преимущество веб-UI: **конфигурация — это часть продукта**, а не редактирование файлов руками.

## Маршруты бэкенда

Канон списка эндпоинтов с request/response: [[web/api-reference]]. Здесь — только архитектурное обоснование группировки.

Группы маршрутов:

- **HTML-страницы** (`GET /`, и v2 — `/catalog`, `/catalog/map`) — Jinja2-рендеринг, отдаются тем же uvicorn, без отдельного фронта.
- **Лоты и состояние** (`/api/lots/*`, `/api/status`) — read-модель из SQLite; пишут только воркеры мониторинга, UI читает.
- **Управление сессией ЕСИА** (`/api/login`) — единственное место, где UI триггерит Playwright (embedded в threadpool, см. [[decisions-log]]).
- **Конфиг** (`/api/config`) — GET/POST одной формой; запись инвалидирует file-watch и перезагружает воркеры без рестарта.
- **Уведомления** (`/api/notifiers/*`) — авто-генерируемые формы из плагинов, см. [[notifications]].
- **Управление циклом** (`/api/cycle/run`, `/api/pause`, `/api/resume`) — UI-команды воркеру мониторинга.
- **Live-канал** (`GET /api/stream`) — SSE-стрим, единственная асинхронная точка в sync-FastAPI.

## Жизненный цикл бэкенда

```
fis-monitor.exe запущен
     │
     ├─ uvicorn слушает localhost:8080
     │
     ├─ Background task — цикл мониторинга
     │  (каждые N минут: fetch → parse → diff → notify через SSE)
     │
     ├─ При старте: попытка автооткрыть браузер на localhost:8080
     │
     └─ SSE-канал отдаёт события подписчикам (открытым вкладкам)
```

## Запуск у пользователя

**Вариант 1 — single exe (как было):**
1. Двойной клик `fis-monitor.exe`
2. Запускается бэкенд, открывается браузер
3. Иконка на рабочем столе ведёт на `localhost:8080`

**Вариант 2 — portable zip (ещё проще):**
1. Извлечь папку
2. Запустить `start.bat` (или `fis-monitor.exe`)
3. Браузер открывается автоматически

**Автозапуск при входе в Windows:**
- Task Scheduler «At Logon» (как раньше)
- Скрытое окно через `pythonw.exe` или `--windowed` флаг Nuitka
- Браузер при автозапуске НЕ открывается (только бэкенд) — пользователь сам кликает по ярлыку, когда нужно

## Forward-compatibility с хостингом (для будущего)

Этот же код в будущем легко развернуть на VPS:
- Заменить `localhost:8080` на доменное имя
- Добавить multi-user (auth, изоляция cookies пользователей)
- HTTPS через Let's Encrypt
- Логин ЕСИА — тот же, только Playwright крутится на сервере

Делать это сразу — рано, см. [[product/risks-legal]] про 152-ФЗ (на сервере мы становимся оператором ПДн со всеми обязательствами). Но **архитектурно дорога открыта**.

## Компоненты UI (hiq3)

### Header — три зоны

Header разделён на три зоны через CSS flex:

- **Левая:** логотип + название + индикатор статуса (pulse-dot, см. ниже).
- **Центральная:** flex-spacer (`margin-left: auto` на правой зоне).
- **Правая:** «Последний новый: X мин назад» → `.header-divider` → иконки (DND, notifications, settings, «Проверить сейчас»).

Разделители: CSS-класс `.header-divider` (1px, `var(--color-border)`). Не inline-style.

Иконки: 32×32, `title` + `aria-label` обязательны. Текст «Не беспокоить» удалён.

### Status indicator — pulse-dot (ADR-050, supersedes ADR-048)

Вместо обратного отсчёта «Проверка через MM:SS» — pulse-dot:

```html
<span class="check-status" data-state="idle|checking">
  <span class="check-dot" aria-hidden="true"></span>
  <span class="check-label">Жду | Проверяю</span>
</span>
```

Состояния переключаются через SSE-события:
- `cycle.started` → JS ставит `data-state="checking"` (анимация pulse запускается)
- `cycle.done`    → JS ставит `data-state="idle"`

Событие `SseCycleStarted(timestamp, cycle_id)` добавлено в `domain/models.py` симметрично `SseCycleDone`.
Поля `next_cycle_mmss`, `next_fire_at`, `next_fire_at_iso` удалены из `SseStatus`.

Подробности: [[decisions/ADR-050-status-indicator-supersedes-countdown|ADR-050]].

### Lot cards — двухколоночный грид

```css
.lot__content-grid { display: grid; grid-template-columns: 1fr auto; align-items: start; }
```

- Контент слева (1fr), actions (кнопка «Открыть») справа, прижаты к верху.
- `@media (max-width: 720px)` — стек, actions снизу.
- Акцентный border-left: класс `.lot-card--new` — оранжевый (`#e67e22`) для `was_new=True`; зелёный дефолт.
- `was_new` передаётся из `LotUserDTO` через `LotViewModel` → шаблон.
- ОГВ: `text-transform: uppercase` убран (только это).
- Дата: `{{ lot.date_create | dateformat }}` через `format_date_ru` (без babel, `web/filters.py`).
- Кадастровый номер: inline `<button data-copy="...">` — переиспользует delegation-обработчик на `document` (`app.js`).

### Filter bar

Отдельный `<div class="filter-bar">` между header и лентой лотов (`_feed_lots.html.jinja`).
- Справа: счётчик лотов (`<span id="feed-lot-count">`).
- Сортировка захардкожена `ORDER BY date_create DESC, id DESC` в `LotFilters` — UI-контрол удалён (см. bd `gektar_monitor-ewqq`). Поле `sort_dir` исключено из `ViewFilters` / `LotFilters` / Form-схемы `/filters/view`. Старые cookies со значением `sort_dir` молча игнорируются (Pydantic `extra="ignore"`).

### Пагинация ленты — кнопка «Показать ещё»

Начальный рендер (`GET /`) загружает не более `_FEED_PAGE_SIZE=200` лотов. Если лотов больше, контекст содержит `next_cursor` (opaque base64 keyset cursor), и шаблон `_feed_lots.html.jinja` рендерит `#load-more-trigger`.

**Кнопка `#load-more-btn`** внутри `#load-more-trigger`:
```html
<div id="load-more-trigger"
     hx-get="/feed/more?cursor=<next_cursor>"
     hx-target="#load-more-trigger"
     hx-swap="outerHTML"
     hx-trigger="click from:#load-more-btn">
  <button id="load-more-btn">Показать ещё</button>
</div>
```

**Endpoint `GET /feed/more?cursor=<opaque>`** (в `routes/main.py`):
- Читает тот же cookie `view_filters`, что и `GET /` — фильтры никогда не расходятся.
- Применяет тот же `only_new` пост-фильтр через `lot_passes_only_new()` (единственный SSOT).
- Рендерит `partials/_feed_more.html.jinja`: карточки лотов + опциональный свежий `#load-more-trigger` для следующей страницы.
- Малformed cursor → 422.

**Cursor chain:** каждый ответ `_feed_more.html.jinja` содержит следующий `next_cursor` в атрибуте `hx-get`. Цепочка заканчивается когда `next_cursor is None` — тогда trigger не рендерится.

**Сосуществование с SSE:** SSE добавляет карточки в начало `#feed` (`afterbegin`), load-more добавляет в конец через outerHTML на `#load-more-trigger`. Пересечений нет — keyset cursor `(date_create DESC, id DESC)` никогда не переиспускает лоты из следующей страницы.

**Что НЕ меняется:** `archive_count` остаётся в контексте (= 0, deprecated) для совместимости шаблонов; будет удалён в отдельном bd.

#### UX-улучшения фильтров

- **C3 onboarding-hint:** при `{% if not filters.subjects %}` — inline-подсказка «Выберите субъекты…».
- **M6 «Очистить фильтры»:** рендерится только при `{% if filters_active %}`.

### Локализация дат

`src/fis_monitor/web/filters.py::format_date_ru` — чистая функция, зарегистрирована
как Jinja2-фильтр `dateformat` в `build_templates()`.  Словарь месяцев встроен
(родительный падеж), без `locale.setlocale`, без babel (~10 МБ экономии, ADR-026).

### Нормализация ВРИ

`src/fis_monitor/infra/normalize.py::normalize_vri` — вызывается в `list_parser.py`
при парсинге, до сохранения в БД.  Whitelist аббревиатур {ИЖС, ЛПХ, СНТ, ДНТ, ОНТ, КФХ}
приводится к `upper()`; остальные строки — первая буква в uppercase, остальные как есть
(не `.capitalize()` — она ломает внутренние заглавные).

См. также: [[product/mvp-scope]], [[product/monitoring-plan]], [[ops/dev-environment]].
