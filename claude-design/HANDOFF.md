# Handoff для Claude Code — Монитор гектара

## Что в пакете

Это **не дизайн-референс**. В пакете лежит готовый фронтенд для приложения мониторинга свободных гектаров на сайте «Дальневосточный гектар»:

- `templates/` — продакшен-шаблоны Jinja2, рассчитанные на FastAPI + HTMX + SSE.
- `static/app.css` — все стили, светлая/тёмная тема, design tokens через CSS-переменные.
- `static/app.js` — клиентский JS без зависимостей: age-ticker, copy, sticky-пилюля, эскалация, контекстное меню, density-toggle, pinned, copy-as-markdown, persist-scroll. Никакой сборки не нужно.
- `README.md` — **главный документ**: мудборд, контракты данных Jinja, карта SSE-событий, описание каждого экрана, описание JS-API. Перед началом работы прочитайте его целиком.

Дополнительно для визуальной сверки:
- `Monitor - main feed.html` — самодостаточный hi-fi демо главного экрана. Открывается в браузере как есть (без сервера) и показывает целевой look-and-feel. Демо-контролы в левом нижнем углу позволяют пощупать flash новых лотов, эскалацию и контекстное меню.
- `Monitor - onboarding.html` — демо 4-шагового онбординга (регионы → SMTP бот-ящика → email получателя → готово).
- `Wireframes.html` — архив исследования с альтернативными лейаутами (нужен только если будете обсуждать редизайн).

## Что нужно дописать (бэкенд)

Стек по брифу: **Python 3.11+, FastAPI, Jinja2, HTMX 1.9, sse-starlette, Playwright headless, SQLite, requests**. Pip, без uv/poetry.

### 1. Скелет приложения

```python
# app/main.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/")
async def feed(request: Request):
    return templates.TemplateResponse("feed.html.jinja", {
        "request": request,
        "settings": ..., "monitor": ..., "dnd": ..., "session": ...,
        "scope": ..., "filters": ..., "filters_active": ...,
        "health": ..., "catchup": ..., "last_cycle": ...,
        "zones": {"hot": [...], "today": [...]}, "archive_count": ...,
    })
```

Полный словарь контекста — в `README.md`, секция «Контракты данных (Jinja)».

### 2. Маршруты (из шаблонов)

Шаблоны уже ссылаются на эти эндпоинты через `hx-post` / `hx-get`. Реализуйте их в FastAPI:

| Метод | Путь | Что делает | Что возвращает |
|---|---|---|---|
| `POST` | `/cycle/run` | Принудительный цикл | 204 (SSE подхватит результат) |
| `POST` | `/dnd` | `{minutes: int}` | 204 + опционально SSE `status` |
| `GET`  | `/dnd/custom` | Открыть форму своего времени | HTML-фрагмент модалки |
| `POST` | `/filters/view` | Применить view-фильтры | новый `<div id="feed">…</div>` |
| `POST` | `/filters/clear` | Сбросить | редирект на `/` |
| `GET`  | `/filters/subjects` | Меню субъектов РФ | HTML-фрагмент |
| `POST` | `/lots/{id}/star` | Звезда | 204 |
| `POST` | `/lots/{id}/archive` | Архивировать diff-карточку | пустая строка (карточка снимется) |
| `POST` | `/lots/{id}/note` | Сохранить заметку | 204 |
| `GET`  | `/lots/{id}/details` | Lazy-detail для list-карточки | HTML-фрагмент `_lot_details.html.jinja` (надо создать) |
| `GET`  | `/lots?page=N` | Постраничная подгрузка архива | новый блок `<section class="zone zone--archive">…</section>` |
| `POST` | `/catchup/dismiss` | Закрыть catch-up банер | пустая строка |
| `GET`  | `/settings` | Экран настроек | HTML (шаблон создать на основе раздела README) |
| `GET`  | `/notifications` | Экран уведомлений | HTML |
| `GET`  | `/history` | История циклов | HTML |
| `GET`  | `/onboarding?step=N` | Шаг мастера | `onboarding/wizard.html.jinja` (готов) |
| `POST` | `/onboarding/save?step=N` | Сохранить шаг | редирект на `?step=N+1` или `/` |
| `POST` | `/onboarding/smtp-test` | Проверка SMTP бот-ящика | HTML-фрагмент чипа результата |
| `GET`  | `/auth/login` | ЕСИА | редирект на Госуслуги |
| `POST` | `/auth/refresh` | Продлить сессию | 204 |
| `GET`  | `/diagnostic.zip` | Архив без секретов | application/zip |

### 3. SSE-эндпоинты

Через `sse-starlette`. Шаблоны подключаются автоматически через `hx-ext="sse" sse-connect="…"`:

| Endpoint | Event | Содержимое event'а |
|---|---|---|
| `/sse/status` | `status` | HTML-фрагмент `_header_status.html.jinja` |
| `/sse/lots` | `lot.new` | HTML-фрагмент `_lot_poster.html.jinja` или `_lot_list.html.jinja` (выбираете на сервере по `temp`) |
| `/sse/lots` | `lot.status` | HTML-фрагмент с `hx-swap-oob` для in-place замены конкретной карточки |
| `/sse/session` | `expired` | пустой div с `hx-swap-oob` снимающим `hidden` с `#session-expired-modal` |

Подробная карта — в `README.md`, секция «SSE — события и фрагменты».

### 4. Бизнес-логика

- **Playwright headless** для логина в ЕСИА (один раз, потом cookies) и получения списка лотов с «Дальневосточный гектар». Сессия — 3 часа, нужен heartbeat и обработка истечения.
- **SQLite** через стандартный `sqlite3` или `aiosqlite`: таблицы `lots`, `cycles`, `notifications`, `state`.
- **Дифф-логика**: каждый цикл сравнивает свежий снимок с предыдущим. Новые лоты → SSE `lot.new`. Исчезнувшие/«зарезервированные» → SSE `lot.status` со значением `event="gone"`.
- **Эскалация** уже реализована на клиенте (`escalationStart()` в `app.js`). Сервер просто шлёт `lot.new` — клиент сам отсчитывает 60с/180с до повышения «громкости» и пульсации title.
- **SMTP бот-ящика** (отдельный почтовый адрес-отправитель) — настраивается в онбординге и в Настройках. Пароль — хранить локально в зашифрованном виде (cryptography.fernet, ключ в `%APPDATA%`). См. README, секция «Открытые вопросы».
- **i18n не нужна**, UI русскоязычный.

### 5. Фильтры (важно!)

Различаются **fetch-time** и **notify-time** и **view-time** — все три уровня используются в разных местах.

- **Fetch-time** (макрорегионы, что складывать в БД): меняется редко, требует докачки данных. Поле в config.
- **Notify-time** (по каким субъектам слать пуш/email): подмножество fetch-scope. Поле в config.
- **View-time** (что показывать в ленте сейчас): сайдбар. Сессия, не сохраняется в config.

Подробнее — в `README.md`.

## Что готово (не трогать)

- Все шаблоны в `templates/` — рассчитаны на контракты данных из README. Менять контекст — менять шаблоны.
- `static/app.css` — все стили централизованы через CSS-переменные. Кастомизация — через переопределение переменных в `:root`, не через хардкод цветов в новых стилях.
- `static/app.js` — модульный IIFE без зависимостей. Все клиентские UX-улучшения уже работают: ticker, copy, flash, escalation chip, sticky pill, density toggle, pinned, copy-as-markdown, context menu, persist-scroll. Расширение — через `window.Monitor.*`.

## Что НЕ готово (шаблонов нет)

Эскизы в `Wireframes.html` уже описывают все экраны. Шаблоны существуют только для **главного** (`feed`) и **онбординга** (`onboarding/`). Остальное — создать по тому же паттерну:

- `templates/settings.html.jinja` — карточки: Расписание, Область наблюдения, Поведение, Сессия ЕСИА.
- `templates/notifications.html.jinja` — каналы (браузер, email, heartbeat, catch-up), notify-фильтр, история отправок.
- `templates/history.html.jinja` — таблица циклов + текстовые виджеты (uptime, тренд).
- `templates/partials/_lot_details.html.jinja` — содержимое для `hx-trigger="revealed once"` в list-карточках.

Стандарт качества — точно как в `feed.html.jinja`: канонический HTML, ARIA-метки, HTMX-биндинги, no inline JS-логики кроме мелких toggle'ов в стиле `onclick`.

## Особенности окружения

- Один пользователь, один процесс, локальный запуск. **Не делайте multi-tenant**, не делайте сессии: `state` — синглтон в памяти + sqlite.
- Запуск: `pip install -r requirements.txt && playwright install chromium && uvicorn app.main:app`. Один порт.
- Тёмная тема через `prefers-color-scheme`. Без явного toggle'а.
- Минимум 1366×768 на ноутбуке. На 1100px сайдбар схлопывается.
- `localStorage` использует ключи `monitor:scroll:*`, `monitor:density`, `monitor:pinned`. `sessionStorage` — только скролл-позиция.

## Чек-лист перед PR

- [ ] Все эндпоинты из таблицы выше реализованы и возвращают именно то, что ждёт шаблон (HTML-фрагмент или 204).
- [ ] SSE-каналы шлют именно те фрагменты, которые описаны в README.
- [ ] При истечении ЕСИА-сессии на ленте остаются читаемые карточки, но `Открыть на сайте` получает `aria-disabled="true"`.
- [ ] Catch-up банер показывается только если предыдущий визит был ≥1 час назад.
- [ ] Health-виджет точно соответствует данным: «Последний цикл» — из `cycles`, «Всего лотов» — `SELECT COUNT(*) FROM lots`, «Последний новый» — `MAX(first_seen_at)`.
- [ ] Тестовое SMTP-письмо отправляется без редиректа (через `hx-post`, ответ — чип результата).
- [ ] Звуки: `static/sfx/double_pop.mp3`, `static/sfx/single_pop.mp3`, `static/sfx/pluck.mp3` (выбрать из CC0 или сгенерировать). Раскомментировать `new Audio(...)` в `playNotificationSound` в `app.js`.

## Открытые вопросы

См. соответствующую секцию в `README.md`. Главные:
1. Где хранить SMTP-пароль бот-ящика?
2. Делать ли «Проверить SMTP» в онбординге обязательным?
3. Tier лота (`tier: 1|2|3` для разных звуков) — определяет сервер или клиент?
