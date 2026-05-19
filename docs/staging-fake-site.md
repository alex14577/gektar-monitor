---
title: Staging fake-site (локальный двойник надальнийвосток.рф)
status: canon
---

# Staging fake-site — локальный двойник надальнийвосток.рф

> Manual-staging инструмент: запускается на dev-машине оператора, имитирует целевой сайт ровно настолько, насколько нужно, чтобы перед отдачей пользователю вручную проверить «появился новый лот → пришло уведомление». Реальные лоты на надальнийвосток.рф появляются редко, поэтому ждать естественного события нерационально.
>
> **Не E2E-автотест.** Долгоживущий процесс, управляется оператором через admin-UI.
>
> **SMTP — настоящий.** Оператор использует свой реальный email (Yandex/Gmail) и проходит обычный [[onboarding]] flow. Никакого MailHog/aiosmtpd.

## Mode of operation

Три процесса, три браузерных вкладки:

```
Терминал 1: python tools/fake_torgi/server.py --port 8765
Терминал 2: FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor
            # (либо python -m fis_monitor --config config.staging.json)

Браузер 1: http://localhost:8765/admin   — admin-UI fake-сайта (добавить/удалить лот)
Браузер 2: http://localhost:8000/        — UI сервиса (SSE-toasts, список лотов)
Почта:     реальный inbox (Yandex/Gmail) — письма от сервиса
```

Сценарий ручной проверки:

1. Запустил fake-сайт.
2. Запустил сервис со staging-base-URL.
3. В UI сервиса прошёл [[onboarding]] (или продолжил с прошлого раза — `state.db` сохраняется): ввёл SMTP-credentials своей почты, recipient = свой email, выбрал регионы.
4. В admin-UI fake-сайта нажал «Добавить лот».
5. Через ≤ `interval_minutes` минут — SSE-toast в браузере сервиса + письмо на реальной почте.
6. Изменил статус существующего лота — повторного письма нет (idempotency).
7. Удалил лот — отрабатывает removal-detection.

Отличие staging от прода — **ровно одна строка** (URL парсимого сайта). Всё остальное (SMTP, регионы, recipients, onboarding) идентично проду — потому что хранится в `state.db` и проходит через тот же UI.

## Изменения в проекте: конфигурационный шов

### Куда ложится `base_url`

| Кандидат | Решение |
|---|---|
| `state.db` (user-editable) | **Нет.** Это операционный параметр инсталляции, не пользовательская настройка. Конфликт с [[decisions/ADR-020-smtp-creds-state-db\|ADR-020]] semantics (SSOT для credentials, не для infra-endpoints). К тому же `state.db` читается после Container-инициализации — HTTP-клиент нужен раньше. |
| Голая env-переменная | **Не как единственный механизм.** Нет документированного default'а рядом с кодом. |
| Pydantic `Settings` + `config.json` | **Да.** Новый sub-model `TargetConfig`, compile-time default (Punycode домен `xn--80aaggvgieoeoa2bo7l.xn--p1ai`) как доменная константа в коде, override через `config.json` (уже читается через `WatchdogConfigSource`). |

### Layered resolution

```
compiled-in defaults  →  config.json (operator)  →  env-override (FIS_TARGET__BASE_URL)
```

Никакого нового механизма (YAML/TOML/XDG) — `config.json` уже есть. Override пути к конфигу через `--config /path/` или `FIS_CONFIG=...` в точке запуска (`__main__.py` / lifespan).

### Prod vs Staging — один файл + env, не два файла

- **Prod build:** `config.json` НЕ содержит ключ `target.base_url` → используется compile-time default (Punycode домен надальнийвосток.рф).
  - Защита: prod URL никогда не попадает в пользовательский конфиг → его нельзя случайно переписать.
- **Staging:** одно из:
  - `FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor` (через Pydantic `env_nested_delimiter="__"`)
  - `python -m fis_monitor --config config.staging.json`, где staging-файл содержит `{"target": {"base_url": "http://localhost:8765"}}`

Два полностью отдельных config-файла, копируемых при сборке, — отвергнуто: хрупко, легко перепутать, нет явного namespacing.

### Что становится конфигурируемым одной пачкой

Пока открываем `TargetConfig` — закрываем сразу всё, что нужно для staging и для prod-операций:

- `target.base_url` — schema/host часть целевого сайта
- `target.request_timeout_seconds` — для медленного fake-сервера и для prod-таймаутов
- `target.user_agent` — сайт может блокировать `python-requests/X.Y`, оператор должен подменять без пересборки

### Что НЕ становится конфигурируемым

- **Endpoint paths** (`/cabinet/free-lot`, `?sort=-DATE_CREATE`, `per-page=10`) — доменные константы, неразрывно связаны с парсером. Если сайт меняет path — меняется и парсер (единица изменения одна). Вынос в конфиг создаёт иллюзию гибкости, которой нет.
- **`smtp_host` defaults** — security-инвариант через [[architecture/03-protocols#SmtpHostPolicy]] и [[decisions/ADR-021-manual-starttls\|ADR-021]]. SSOT — `state.db` ([[decisions/ADR-020-smtp-creds-state-db\|ADR-020]]).
- **Playwright host-whitelist** (ADR-011) — security-инвариант.

### DI-композиция: `TorgiUrlBuilder`

`HttpClient` — generic Protocol, принимает полный URL в каждом вызове. `base_url` в него прокидывать нельзя — нарушит шов. Варианты:

| Вариант | Оценка |
|---|---|
| `base_url: str` прямо в конструктор `PollingService` | Размазывает знание URL-структуры по сервисам. |
| `FisHttpClient` обёртка над `HttpClient` | Ломает Protocol-смысл: generic vs site-specific. |
| **`TorgiUrlBuilder` value-object** | **Выбран.** Один источник URL-логики. |

`TorgiUrlBuilder` принимает `base_url`, предоставляет методы `lot_list_url(region, page)`, `lot_detail_url(lot_id)`. Endpoint paths — module-level константы рядом с builder'ом. Сервисы зависят от `TorgiUrlBuilder` + `HttpClient`, оба инжектируются. Пересборка builder-а происходит только на рестарт сервиса (hot-reload — future work, ADR-024).

### Артефакт `smtp_host="smtp.yandex.ru"` (models.py:372)

Default в Pydantic-модели противоречит [[decisions/ADR-020-smtp-creds-state-db\|ADR-020]] (SSOT в `state.db`). Чистка одновременно с введением `TargetConfig`:

- `EmailConfig.smtp_host: str | None = None`
- Константа `DEFAULT_SMTP_HOST = "smtp.yandex.ru"` живёт в `infra/smtp/constants.py`, не в доменной модели

### Реализованные изменения в проекте (ADR-024)

1. `src/fis_monitor/domain/models.py` — добавлен `TargetConfig` (base_url / request_timeout_seconds / user_agent) как sub-model в `Settings`; убран `smtp_host="smtp.yandex.ru"` default из `EmailConfig`.
2. `src/fis_monitor/infra/http/` — реализованы `TorgiUrlBuilder` + endpoint paths как module-level константы.
3. `src/fis_monitor/infra/config_source.py` — добавлена поддержка `FIS_CONFIG` env / `--config` CLI override.
4. `src/fis_monitor/infra/smtp/constants.py` — `DEFAULT_SMTP_HOST` перенесён сюда из models.
5. Composition root — `TorgiUrlBuilder` инстанциируется из `config_source.current().target.base_url`, инжектируется в сервисы (MonitorCycleService, FullScanService, EnrichmentService).

## Fake-сайт MVP

### Stack — ноль новых зависимостей

- **FastAPI** + **Jinja2** + **uvicorn** — уже в prod-deps. `http.server` не понимает query-params нативно, Flask тащит лишнюю зависимость.

### Файловая структура

```
tools/
  fake_torgi/
    server.py           # FastAPI app + uvicorn entry-point
    lots.json           # persistence (gitignored, есть .example)
    templates/
      list.html         # shell из tests/fixtures/list_region1_perpage50.html
      detail.html       # shell из tests/fixtures/detail_lot_9990.html
      admin.html        # форма + список лотов
```

Вне `src/`, без `__init__.py` в `tools/`. **Не импортируется из prod-кода**, изоляция обеспечивается import-linter контрактами ([[decisions/ADR-006-import-linter\|ADR-006]]).

### Хранение состояния — JSON-файл

`tools/fake_torgi/lots.json` — золотая середина:

- **Persistence через перезапуск.** Сценарий «утром добавил лоты, после обеда проверил» — единственный реалистичный workflow. In-memory это убивает.
- **Редактируется вручную.** Хочешь батч из 10 лотов — копируешь JSON.
- **SQLite overkill.** 5-20 лотов, нет конкурентного доступа, нет транзакций.

Стартовый `lots.json.example` с 3-5 лотами из реальных фикстур → сразу есть с чем работать.

### HTML-генерация — фикстуры как shell

Берём существующие `tests/fixtures/list_region1_perpage50.html` и `detail_lot_9990.html` как **shell-шаблоны**: header / footer / CSS / скрипты остаются нетронутыми, динамически подменяется только `<tbody>` (список) или `.request-declaration__block-main` (detail).

Это критично для **fidelity парсера**: `selectolax` ищет `tr[data-key]`, `td[data-col-seq]` — если шаблон воспроизводит ровно эту структуру, парсер не различает fake от real.

Склейка строк — нет (экранирование, кириллица, путь в ад). Jinja2 уже используется в `fis_monitor.web`.

### Admin-UI — server-rendered, без JS

`GET /admin` отдаёт страницу: форма «Добавить лот» (id, cadastral_no, area_sqm, region, status select, date_create) + список существующих, у каждого кнопки «Удалить» / «Изменить статус» как отдельные маленькие `<form method="POST">`.

После POST — **redirect-after-POST** на `/admin` (PRG-паттерн, чтобы F5 не дублировал).

**Никакого JS, никакого HTMX, никакого React.** ~50 строк Jinja2.

### Fault-injection — НЕ в MVP

Отложить полностью. До первого реального использования непонятно, нужно ли вообще. Если понадобится — toggle через checkbox в admin-UI + одна проверка в handler.

### Endpoints (минимум, MVP)

| Endpoint | Назначение |
|---|---|
| `GET /cabinet/free-lot?region=N` | Список лотов из `lots.json`, отфильтрованный по region. Возвращает HTML по template `list.html`. |
| `GET /cabinet/free-lot-view?id=N` | Detail-карточка лота N. HTML по `detail.html`. |
| `GET /admin` | Admin-UI: форма + список. |
| `POST /admin/lots` | Добавить лот (form-data → `lots.json`). |
| `POST /admin/lots/{id}/delete` | Удалить лот. |
| `POST /admin/lots/{id}/status` | Изменить статус. |

Cookie-auth для `/cabinet/*` — на старте **не нужна**. Реальный сайт требует `PHPSESSID`, но fake-сайт принимает любые запросы. Если staging-тест должен покрыть «сессия истекла» — добавить позже toggle.

### Запуск

```bash
python tools/fake_torgi/server.py --port 8765
```

- `argparse` (stdlib), default port = 8765
- `if __name__ == "__main__": uvicorn.run(app, host="127.0.0.1", port=args.port)`
- Console-script в `pyproject.toml` — **нет**, засоряет prod namespace
- `python -m tools.fake_torgi` — **нет**, потребует `__init__.py` и сломает изоляцию

Альтернатива для разработки самого fake-сайта (auto-reload):
```bash
uvicorn tools.fake_torgi.server:app --port 8765 --reload
```

### Документация

Секция **«Local staging»** в основном `README.md` проекта. Не отдельный файл в `tools/`, не в Obsidian — потому что `README.md` — первое, что открывает человек через 3 месяца.

Содержание секции: как запустить, URL admin-UI, как сконфигурировать сервис (`FIS_TARGET__BASE_URL=...`), ссылка на этот документ (`docs/staging-fake-site.md`).

### Honest MVP-список (обязательно для v1)

1. `GET /cabinet/free-lot?region=N` с точной HTML-структурой (`tr[data-key]` + 16 `td[data-col-seq]`)
2. `GET /cabinet/free-lot-view?id=N` с точной structure `.request-declaration__block-main`
3. `GET /admin` + `POST /admin/lots` — форма + список + удаление
4. `lots.json` persistence
5. `python tools/fake_torgi/server.py --port 8765` без extra `pip install`

### Что добавить через месяц (если реально понадобится)

- «Изменить статус лота» одним кликом из admin-таблицы (сейчас через delete + re-add)
- `GET /admin/seed` — загрузить пачку лотов из реальных фикстур (для стресс-теста парсера)
- Fault-injection toggle (`503 на 30 сек`, `slow response`, `malformed HTML`)
- `?perPage=50` и сортировка, если PollingService их реально использует в URL
- Cookie-auth (`PHPSESSID` + `expire_session` toggle) для проверки re-login сценария

## Связь с архитектурой

- [[architecture/03-protocols]] — `HttpClient` Protocol остаётся generic; URL-логика концентрируется в `TorgiUrlBuilder` (value-object, [[decisions/ADR-024-target-config-and-url-builder|ADR-024]])
- [[architecture/04-composition-root]] — Container получает `base_url` из `ConfigSource.current().target.base_url`, создаёт `TorgiUrlBuilder`
- [[onboarding]] — staging проходит обычный onboarding flow (ADR-018), SMTP-credentials оператора → `state.db`
- [[decisions/ADR-020-smtp-creds-state-db\|ADR-020]] — SMTP SSOT в `state.db`, не дублируется в config-файле
- [[decisions/ADR-006-import-linter\|ADR-006]] — `tools/` изолирован от `src/fis_monitor/` через import-linter контракты

## Принятые решения

- **Имя env-переменной:** `FIS_TARGET__BASE_URL` — Pydantic nested delimiter `__`, согласован с моделью `TargetConfig` (ADR-024).
- **`config.staging.json`:** не под git; оператор создаёт локально. `lots.json.example` — под git как стартовый образец.
- **`lots.json`:** не под git (`.gitignore`). Только `lots.json.example` в репозитории.
- **ADR:** решение зафиксировано в [[decisions/ADR-024-target-config-and-url-builder|ADR-024]]; отдельный ADR на staging-fake-site не создавался — не содержит архитектурных решений сверх ADR-024.
