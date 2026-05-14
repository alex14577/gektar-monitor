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
