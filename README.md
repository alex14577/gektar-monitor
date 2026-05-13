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

## CI / Quality gates

```bash
pytest          # unit + integration tests
ruff check src  # linting
lint-imports    # layered architecture contracts (ADR-006)
```

Контракты `layers` и `domain_purity` определены в `.importlinter` и проверяются тестом `tests/test_import_linter_contracts.py`.
