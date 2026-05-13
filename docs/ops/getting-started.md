# Getting Started (Day 1 разработчика)

Цель: за 30–60 минут получить локально работающий мониторинг с авторизованной сессией ЕСИА и пройденными тестами.

## Требования

- **Python 3.12+** (использование `match`, `Self`, новый typing).
- **git**.
- **ОС для разработки**: Linux (Ubuntu 22.04+ / Debian 12).
- **ОС для приёмочных тестов клиентского сценария**: Windows 10/11 (VM подойдёт). На Windows проверяем single-instance lock, путь `%LOCALAPPDATA%`, поведение Notification API.
- Браузер (Chromium) — будет установлен Playwright автоматически.

## Установка

```bash
git clone <repo-url> fis-monitor
cd fis-monitor

python3.12 -m venv .venv
source .venv/bin/activate   # Linux
# .venv\Scripts\activate    # Windows

pip install -U pip
pip install -r requirements.txt
playwright install chromium
```

**Зафиксированный стек и состав `requirements.txt`** — см. [[decisions-log]] → раздел «Технологический стек», блок requirements.txt.

Кратко: SQLite — встроенный `sqlite3` (без `aiosqlite`). HTTP — `requests`. HTML-парсер — `selectolax`. Handlers FastAPI пишем как `def ...` (sync, FastAPI сам разнесёт по threadpool).

## Получение тестовой сессии ЕСИА

Сессия живёт в `profile/` (persistent context Playwright). Логин делает **сам клиент** руками в открытом окне — приложение никогда не видит и не хранит пароль ЕСИА.

```bash
python -m fis_monitor.auth.playwright_login
```

Что происходит:
1. Поднимается Playwright в **headed** режиме с `user_data_dir=./profile`.
2. Открывается `НаДальнийВосток.рф` → клиент жмёт «Войти через Госуслуги».
3. Клиент проходит ЕСИА (логин, пароль, 2FA — всё в окне браузера).
4. После редиректа обратно на ФИС окно можно закрывать. Cookies сохранены в `profile/`.

> **Никогда не передавай пароль ЕСИА в скрипт, env-переменную или конфиг.** Только интерактивный ввод в окне браузера.

См. [[authentication]] для деталей.

## Запуск dev-сервера

```bash
uvicorn fis_monitor.app:app --reload --port 8080 --host 127.0.0.1
```

Сервер слушает **только loopback**. Открыть UI: <http://127.0.0.1:8080>.

В dev-режиме монитор-цикл стартует с укороченным интервалом (см. `config.dev.json`). Логи: stdout + `logs/app.jsonl`.

## Тесты

```bash
pytest tests/unit            # парсеры, sort-strategy, идемпотентность
pytest tests/integration     # с реальной SQLite, без сети
pytest --fixtures            # список фикстур
```

Фикстуры HTML (`tests/fixtures/`) — снапшоты страниц сайта от 12.05.2026. Регрессия парсера: при апгрейде селекторов прогон всех фикстур должен дать точно тот же output. См. [[decisions-log]] → «Тесты».

## Структура папок

См. [[project-structure]].

## Что дальше

- [[runbook]] — что делать, когда что-то ломается.
- [[decisions-log]] — все принятые архитектурные решения, источник истины.
- [[mvp-scope]] — что входит и не входит в MVP.

## См. также

- [[project-structure]]
- [[runbook]]
- [[authentication]]
- [[decisions-log]]
