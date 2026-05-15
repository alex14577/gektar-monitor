# fis-monitor

Десктопное приложение для мониторинга свободных гектаров на сайте «Дальневосточный гектар».

Документация: см. [`docs/`](docs/), точка входа — [`docs/index.md`](docs/index.md).
Контекст текущей сессии: [`docs/SESSION-RESUME.md`](docs/SESSION-RESUME.md).

## Быстрый старт (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn fis_monitor.app:app --reload
```

Открыть: <http://127.0.0.1:8000/>.

## Стек

Python 3.12+, FastAPI, SQLite (sync, WAL), Pydantic v2, selectolax, requests,
Playwright (embedded threadpool), Jinja2, HTMX, sse-starlette, platformdirs.

Полный список и обоснование — в [`docs/decisions-log.md`](docs/decisions-log.md).

## Релизы

```bash
# Тегированный релиз
git tag v0.1.0
git push origin v0.1.0
# → GitHub Actions соберёт обе платформы и создаст Release

# Ручной запуск без тега
# Actions tab → release workflow → Run workflow
# Артефакты доступны 90 дней в Actions UI
```

> Первый запуск будет долгим: кэш Playwright Chromium (~280 MB) пустой,
> он скачивается при каждом новом ключе кэша.

## CI / Quality gates

```bash
pytest          # unit + integration tests
ruff check src  # linting
lint-imports    # layered architecture contracts (ADR-006)
```

Контракты `layers` и `domain_purity` определены в `.importlinter` и проверяются тестом `tests/test_import_linter_contracts.py`.

## Local staging (fake-torgi)

`tools/fake_torgi/` — локальный staging-сервер, зеркалирующий торги.gov.ru достаточно для ручного E2E-прогона: оператор добавляет лот → сервис опрашивает → отправляет email + SSE.

```bash
# Запуск (нулевые новые зависимости — FastAPI+Jinja2+uvicorn уже в prod-deps)
python tools/fake_torgi/server.py --port 8765

# Указать сервису на fake-сервер
FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor

# Открыть admin UI для добавления/удаления лотов
# http://localhost:8765/admin

# Health-check
curl http://localhost:8765/status
```

Эндпоинты:

| Endpoint | Назначение |
|---|---|
| `GET /cabinet/free-lot?region=N` | Список лотов (HTML, SelectolaxListParser) |
| `GET /cabinet/free-lot-view?id=N` | Карточка лота (HTML, SelectolaxDetailParser) |
| `GET /admin` | Admin UI — список лотов |
| `POST /admin/lots` | Добавить лот (PRG) |
| `POST /admin/lots/{id}/delete` | Удалить лот (PRG) |
| `POST /admin/lots/{id}/status` | Изменить статус (PRG) |
| `GET /status` | Health-check JSON |

Данные хранятся в `tools/fake_torgi/lots.json` (gitignored). Пример: `tools/fake_torgi/lots.json.example`.
