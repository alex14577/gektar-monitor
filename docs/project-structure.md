# Структура проекта

Раскладка кода — **рабочая гипотеза**. Конкретные имена модулей и разбиение могут меняться. Финализация после выбора тех-стека.

## Дерево

```
src/fis_monitor/
  __init__.py
  app.py                # FastAPI entry, lifespan, монтаж роутов и SSE
  config.py             # Pydantic-модели config.json, валидация, file-watch hook
  data_model.py         # Реализация Pydantic-моделей из docs/data-model.md
                        # (Lot, LotUserState, LotDTO, SSE*, CycleResult, OnboardingState, ...)

  db/
    schema.sql          # Канон схемы (копия/symlink на docs/db/schema.sql):
                        # mirror + user-state + FTS5 + R-tree + smtp_credentials
    migrations/         # Версионные миграции (alembic или своё)
    repository.py       # CRUD: lots, lot_history, notifications, cycles, settings

  monitor/
    cycle.py            # monitor_cycle background task, planning + backoff
    parser_list.py      # Парсер /cabinet/free-lot (список, 10 на страницу)
    parser_detail.py    # Парсер /cabinet/free-lot-view (карточка лота)
    sort_strategy.py    # early-exit: sort=-DATE_CREATE, остановка на last_known_id

  enrichment/
    worker.py           # Фоновый enrichment до 10 параллельно, приоритет ниже monitor

  notifiers/
    base.py             # Notifier ABC: send(lot, event) -> Result
    email.py            # SMTP (бот-ящик по умолчанию + override клиентский)
    browser.py          # Notification API через SSE → клиентский JS
    registry.py         # Регистрация и discovery плагинов

  auth/
    playwright_login.py # Headed login через ЕСИА в persistent context (profile/)
    session.py          # Проверка валидности сессии, триггер перелогина

  web/
    routes/             # API endpoints (FastAPI routers)
      lots.py
      settings.py
      auth.py
      notifications.py
      diagnostics.py
    sse.py              # SSE-стрим: `queue.Queue` (thread-safe) + sync→async через
                        # `await loop.run_in_executor(None, q.get)`. Multi-tab fan-out:
                        # один источник → N очередей подписчиков (по вкладкам).
                        # См. decisions-log → «SSE мост sync→async».
    csrf.py             # Origin-header + cookie-token middleware
    static/             # CSS/JS, HTMX-инициализация
    templates/          # Jinja2 layouts и фрагменты

  utils/
    paths.py            # Обёртка над platformdirs: user_data_dir, user_config_dir, cache_dir
    lock.py             # single-instance: paths.data_dir / "app.lock" + PID
    logging.py          # Структурные JSON-логи, ротация посуточно 30 дней
    timezone.py         # МСК-канон + опциональный display-TZ

  autostart/
    __init__.py         # Выбор по sys.platform
    windows.py          # Task Scheduler At-Logon через schtasks
    linux.py            # XDG Autostart: ~/.config/autostart/fis-monitor.desktop

tests/
  fixtures/             # HTML-снапшоты сайта (датированные)
  unit/                 # Парсеры, sort-strategy, идемпотентность, lock
  integration/          # SQLite + FastAPI TestClient, без сети
```

## Описание модулей

- **`app.py`** — точка входа. Создаёт FastAPI, монтирует роуты, стартует фоновые таски (monitor cycle, enrichment worker, file-watch конфига), биндит на `127.0.0.1`.
- **`config.py`** — Pydantic-модели для `config.json`. Один источник правды по дефолтам и валидации. File-watch перезагружает без рестарта.
- **`db/repository.py`** — все SQL в одном месте. Разделение mirror (можно стереть) и user-state (бережём). См. [[db/schema|db/schema.sql]].
- **`monitor/cycle.py`** — оркестратор цикла: запросить первую страницу, разобрать, найти новые ID, посчитать backoff при 5xx, записать `cycles`.
- **`monitor/parser_list.py` / `parser_detail.py`** — парсинг HTML. Изолированы от сетевого слоя, тестируются на фикстурах.
- **`monitor/sort_strategy.py`** — алгоритм early-exit и защита от регрессии ID (см. [[parser/sort-strategy]] и [[product/monitoring-plan]] → «Защита от смены ID-схемы»).
- **`enrichment/worker.py`** — фоновое дозаполнение карточек, очередь с приоритетом ниже монитора, до 10 параллельно. См. [[product/monitoring-plan]].
- **`notifiers/*`** — плагин-архитектура. `base.Notifier` — ABC, конкретные каналы регистрируются через `registry`. Идемпотентность по `(lot_id, channel)`. См. [[notifications]].
- **`auth/playwright_login.py`** — открывает headed Chromium с persistent context, ждёт пока клиент пройдёт ЕСИА сам.
- **`auth/session.py`** — детектит 302 на login, поднимает флаг «нужен релогин», останавливает enrichment.
- **`web/routes/`** — API-эндпоинты по доменам (лоты, настройки, авторизация, уведомления, диагностика).
- **`web/sse.py`** — server-sent events: пуш новых лотов в открытую вкладку UI.
- **`web/csrf.py`** — проверка `Origin` + secure-cookie токен. См. [[architecture]] → §1 CSRF middleware.
- **`utils/paths.py`** — единая точка для всех путей данных. Использует `platformdirs`: на Windows → `%LOCALAPPDATA%\fis-monitor\`, на Linux → `~/.local/share/fis-monitor/`. Никакого хардкода `%LOCALAPPDATA%` в остальном коде.
- **`utils/lock.py`** — single-instance защита через PID-файл с авто-захватом «осиротевшего» lock.
- **`utils/logging.py`** — структурный JSON-логгер, ротация, отдельный `requests.jsonl` для журнала запросов.
- **`autostart/`** — кросс-платформенный автозапуск. `__init__.py` диспатчит на windows/linux по `sys.platform`. В MVP реализована Windows-ветка (Task Scheduler), Linux-ветка — заглушка под будущий хостинг.

## Тесты

- **`tests/fixtures/`** — датированные HTML-снапшоты (`cabinet-free-lot-2026-05-12.html`, `cabinet-free-lot-view-<id>-2026-05-12.html`). При апгрейде парсера полная регрессия должна давать тот же output.
- **`tests/unit/`** — парсеры, sort-strategy, lock-файл, идемпотентность нотификатора.
- **`tests/integration/`** — FastAPI TestClient + SQLite, без сети. Покрывают CSRF, SSE, API.

## Конфигурация и данные (вне `src/`)

Пути берутся из `utils/paths.py` (через `platformdirs`):

| Файл/папка | Windows | Linux |
|---|---|---|
| `config.json` | `%LOCALAPPDATA%\fis-monitor\` | `~/.config/fis-monitor/` |
| `state.db`, `profile/`, `logs/`, `app.lock` | `%LOCALAPPDATA%\fis-monitor\` | `~/.local/share/fis-monitor/` |

Структура внутри директории идентична:
```
state.db                    # SQLite, WAL
profile/                    # Playwright persistent context (cookies ЕСИА)
logs/
  app.jsonl                 # ротация посуточно, 30 дней
  requests.jsonl
app.lock                    # PID single-instance
```

## Сборка артефактов

Nuitka не умеет кросс-компиляцию. Сборка обоих бинарей через CI:

| Платформа | Раннер CI | Артефакт |
|---|---|---|
| Windows | `windows-2022` | `fis-monitor.exe` |
| Linux | `ubuntu-22.04` | `fis-monitor` (ELF) |

Релиз клиенту — только Windows. Linux-бинарь — для разработки и будущего хостинга (см. [[decisions-log]] → forward-compat).

## Зафиксированный стек

См. [[decisions-log]] → «Технологический стек». Кратко:

- **Python 3.12+**
- **Pydantic v2** — модели `config.json` и NotifierConfig (плагин-схемы)
- **sqlite3** sync (встроенный) — без `aiosqlite`. Один коннект на поток,
  `PRAGMA busy_timeout=5000` на каждом коннекте
- **requests** sync — HTTP-клиент. Параллельность enrichment через
  `concurrent.futures.ThreadPoolExecutor` (до 10 тредов)
- **selectolax 0.4.8** — HTML-парсер (верифицирован на фикстурах 12.05.2026)
- **playwright 1.58.0** + Chromium 145 (Chrome for Testing). Embedded в FastAPI
  threadpool, не subprocess. Fallback на 1.56.0 если ЕСИА flag-ает CfT
  (см. [[ops/runbook]] сценарий 9)
- **FastAPI handlers как `def ...`** (sync), не `async def`. FastAPI разносит по
  threadpool сам
- **CSRF**: своя минимальная middleware в `web/csrf.py` (`Origin` + `Host` +
  `X-CSRF-Token`), ~30 строк, без внешних зависимостей
- **SSE**: `sse-starlette` (`EventSourceResponse`) в `web/sse.py`. Sync→async
  мост через `queue.Queue` + `run_in_executor`, multi-tab fan-out
- **jinja2** — серверный рендер шаблонов; tier/freshness считает сервер и
  кладёт в `data-tier`/`data-freshness` атрибуты карточки
- **platformdirs** — кросс-платформенные пути для данных и конфига
  (`~/.local/share/fis-monitor/` на Linux, `%LOCALAPPDATA%\fis-monitor\` на
  Windows)
- **watchdog** — file-watch на `config.json` для hot-reload без рестарта
- **psutil** — PID-проверка для single-instance lock
- **SMTP-учётка** — `state.db` (таблица `smtp_credentials`), **не** `config.json`

## См. также

- [[ops/getting-started]]
- [[decisions-log]]
- [[product/monitoring-plan]]
- [[web/ui-architecture]]
