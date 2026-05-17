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
| `GET /cabinet/free-lot?region=N&page=K&per-page=M` | `SelectolaxListParser` — `tr[data-key]`, 14+ `td[data-col-seq]`. Поддерживает пагинацию: `page` (1-based) + `per-page` (slice). `.table-paginate__info` всегда показывает полный total (как на real-сайте) — парсерский `total_count` стабилен. Когда `page` за пределами — пустой `<tbody>` (стоп-сигнал для `PaginatedListFetcher.iterate()`). |
| `GET /cabinet/free-lot-view?id=N` | `SelectolaxDetailParser` — `.request-declaration__block-main` |
| `GET /admin` | Admin UI (HTML, PRG-форма) |
| `POST /admin/lots` | Добавить лот, редирект на `/admin` |
| `POST /admin/lots/{id}/delete` | Удалить лот, редирект на `/admin` |
| `POST /admin/lots/{id}/status` | Изменить статус, редирект на `/admin` |
| `GET /status` | Health-check: `{"ok": true, "lots": N, "server": "fake-torgi"}` |
| `GET /cabinet/` | Stub cabinet (за `SessionMiddleware`) — точка landing после fake-ESIA login |
| `GET /fake-esia/authorize?redirect_uri=…` | Минимальная HTML-страница с кнопкой «Войти» |
| `POST /fake-esia/login` (form: `redirect_uri`) | Выдаёт cookie `fis_session=…` (HttpOnly, SameSite=Lax), 302 на `redirect_uri` |

### Fake-ESIA flow

`/cabinet/*` защищён `SessionMiddleware` — без валидного `fis_session` cookie запросы перенаправляются на `/fake-esia/authorize`. Цепочка для headed Playwright:

1. `GET /cabinet/` без cookie → 302 на `/fake-esia/authorize?redirect_uri=/cabinet/`
2. Юзер жмёт «Войти» → `POST /fake-esia/login` → 302 + Set-Cookie на `/cabinet/`
3. `GET /cabinet/` с cookie → 200 (Playwright `wait_for_url('**/cabinet/**')` завершается успехом)

`redirect_uri` проходит через `_safe_redirect_uri()` — отвергаются абсолютные URL (scheme/netloc), backslash-trick (`/\evil.com`), path traversal (`..` сегменты). Session-store — in-memory dict с Lock, TTL не реализован (dev-only). `/admin` и `/status` middleware не трогает — это публичные dev-endpoint-ы.

### Auth bypass для headless-CI

Если установить `FAKE_TORGI_NO_AUTH=1` (truthy: `1`/`true`/`yes`/`on`) — `SessionMiddleware` пропускает все `/cabinet/*` запросы без проверки cookie. Это нужно когда Playwright headed-окно недоступно (headless WSL без DISPLAY, CI-окружение): `monitor_cycle` сразу получает lot-list HTML и тестирует полный pipeline без шага login. Env-var читается per-request — можно тоглить рантайм. В `scripts/run_e2e_stack.sh` пробрасывается из `E2E_NO_AUTH=1`.

## Как запустить

```bash
python tools/fake_torgi/server.py --port 8765
FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor
# Admin UI: http://localhost:8765/admin
```

Удобнее — через dev-скрипт `scripts/run_e2e_stack.sh`: поднимает fake_torgi + fis-monitor одной командой, ждёт health-check, печатает адреса. См. `[[scripts]]` (skрипт сам по себе).

## Конфигурируемый login-URL

`PlaywrightLoginSession.__init__(login_start_url=…)` принимает полный URL login-старта. Composition вычисляет `f"{base_url}/cabinet/"` + derive-ит `allowed_hosts` через `composition._derive_login_config(base_url)`. Heuristic: hostname ∈ `{127.0.0.1, localhost}` → loopback-only allowlist (без gosuslugi); иначе — prod allowlist. Это держит Playwright-host-allowlist концерны вне `TargetConfig` (см. [[../decisions/ADR-024-target-config-and-url-builder|ADR-024]]).

## Персистентность

- `tools/fake_torgi/lots.json` — текущие лоты (gitignored)
- `tools/fake_torgi/lots.json.example` — три примера лотов (committed)

## Изоляция (ADR-006)

`tools/fake_torgi/server.py` **не импортирует** `fis_monitor`. Контракт проверяется тестом `tests/integration/test_fake_torgi_smoke.py::test_server_does_not_import_fis_monitor` через AST-анализ.

## Нулевые новые зависимости

FastAPI, Jinja2, uvicorn уже в `[project.dependencies]`. Новых пакетов не добавлено.
