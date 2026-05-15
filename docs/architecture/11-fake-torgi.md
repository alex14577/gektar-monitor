---
title: fake-torgi staging server
created: 2026-05-15
---

# fake-torgi staging server

## Назначение

`tools/fake_torgi/` — локальный staging-сервер для ручного E2E-прогона цикла:

> оператор добавляет лот в admin-UI → `fis_monitor` опрашивает → отправляет email + SSE-toast

Не является частью продакшн-пакета. Файлы лежат в `tools/`, **не** в `src/`.

## Эндпоинты

| Endpoint | Парсер / потребитель |
|---|---|
| `GET /cabinet/free-lot?region=N` | `SelectolaxListParser` — `tr[data-key]`, 14+ `td[data-col-seq]` |
| `GET /cabinet/free-lot-view?id=N` | `SelectolaxDetailParser` — `.request-declaration__block-main` |
| `GET /admin` | Admin UI (HTML, PRG-форма) |
| `POST /admin/lots` | Добавить лот, редирект на `/admin` |
| `POST /admin/lots/{id}/delete` | Удалить лот, редирект на `/admin` |
| `POST /admin/lots/{id}/status` | Изменить статус, редирект на `/admin` |
| `GET /status` | Health-check: `{"ok": true, "lots": N, "server": "fake-torgi"}` |

## Как запустить

```bash
python tools/fake_torgi/server.py --port 8765
FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor
# Admin UI: http://localhost:8765/admin
```

## Персистентность

- `tools/fake_torgi/lots.json` — текущие лоты (gitignored)
- `tools/fake_torgi/lots.json.example` — три примера лотов (committed)

## Изоляция (ADR-006)

`tools/fake_torgi/server.py` **не импортирует** `fis_monitor`. Контракт проверяется тестом `tests/integration/test_fake_torgi_smoke.py::test_server_does_not_import_fis_monitor` через AST-анализ.

## Нулевые новые зависимости

FastAPI, Jinja2, uvicorn уже в `[project.dependencies]`. Новых пакетов не добавлено.
