---
id: ADR-025
title: "SSE routing — единственный эндпоинт /events"
status: accepted
date: 2026-05-14
---

## Context

Шаблоны (`base.html.jinja`, `feed.html.jinja`) подключались к `/sse/status`, `/sse/session`,
`/sse/lots` — трём несуществующим роутам. Бекенд экспортировал только `GET /events`
(`web/routes/events.py`). Результат: непрерывный поток `404` в uvicorn-логе.

Два варианта исправления:
- **Вариант A**: один общий поток `/events`, фильтрация на стороне клиента через `sse-swap`.
- **Вариант B**: три отдельных роута `/sse/status`, `/sse/session`, `/sse/lots`.

## Decision

Принят **Вариант A**: единственный роут `GET /events`.

Все три `sse-connect` в шаблонах изменены на `/events`. `sse-swap` остаётся (`status`,
`expired`, `lot.new,lot.status`) — HTMX SSE extension фильтрует входящие фреймы по
`event: <name>` из SSE-потока и применяет swap только к совпадающему имени.

## Rationale

1. **SseStreamer уже работает как fanout**: все события типизированы полем `event`
   (`Literal["lot.new"]`, `Literal["session.expired"]`, …) и попадают в один поток.
   Разбивать по роутам — это дублировать тот же поток трижды без реальной пользы.

2. **HTMX SSE extension поддерживает multi-event filtering**: несколько элементов с
   разными `sse-swap` на одной странице могут читать один и тот же `sse-connect` URL;
   каждый реагирует только на своё имя события. Это документированная возможность
   расширения.

3. **Единый Origin-check**: логика проверки заголовка `Origin` (DNS-rebinding protection)
   живёт в одном месте (`web/routes/events.py`). Вариант B потребовал бы либо дублирования,
   либо вынесения в middleware — лишнее coupling.

4. **Onboarding-gate**: `_WHITELIST_PREFIXES` содержит один префикс `/events` вместо
   старого `/sse/`. Проще, без ambiguity.

## Consequences

- Templates не знают о внутренней топологии событий — только о URL `/events` и именах
  событий, которые они слушают. Добавление нового типа события не требует нового роута.
- `sse-swap="status"` и `sse-swap="expired"` не совпадают с реальными event-именами домена
  (`cycle.error`, `smtp.failed`, `session.expired`). Это — отдельная задача по выравниванию
  имён; данный ADR исправляет только URL-маршрутизацию (P1 bug).
- `docs/architecture/07-concurrency.md` §7.3 обновлён: onboarding-gate покрывает `/events`.
