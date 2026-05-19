---
title: Server-performance v2 — расширенное исследование
status: complete
date: 2026-05-14
---

# Server-performance v2 — расширенное исследование

## TL;DR

1. **Весь `/cabinet/` требует авторизации** — 302 → ESIA/Госуслуги. Анонимный доступ к лотам невозможен.
2. **ETag/Last-Modified отсутствуют**: `Cache-Control: no-store, no-cache` на всех страницах — оптимизация через 304 недоступна.
3. **GZIP работает**: список (~268 KB HTML) сожмётся до ~27 KB — 10× выигрыш в трафике. Сервер возвращает `Vary: Accept-Encoding`.
4. **Sort `-DATE_CREATE` и `-DATE_UPDATE` доступны** как параметры (Yii2-паттерн: минус = DESC). Это ключ к early-exit стратегии.
5. **Export endpoint есть** (`/cabinet/start-export-freelot`, `/export-file/check-export`), но оба требуют сессии — нет обходного пути.

---

## Сравнение с baseline (server-performance.md, 12.05.2026)

| Метрика | Было (12.05.2026) | Стало (14.05.2026) | Разница |
|---|---|---|---|
| Список `/cabinet/free-lot?region=1&per-page=50` | 151 с | **не измерено** (302 без auth) | н/д — нужна сессия |
| Редирект `/cabinet/free-lot` → `/default/login` | н/д | **~330 мс** | новая метрика |
| Главная страница `/` (публичная) | н/д | **1.2–1.7 с** | новая метрика |
| `per-page` cap | 50 | 50 (подтверждено из fixture) | без изменений |
| Анонимный доступ к лотам | неизвестно | **нет (302 → ESIA)** | подтверждено |
| ETag/If-Modified-Since | не проверялось | **не поддерживается** | новая находка |
| Сжатие GZIP | не проверялось | **работает: 268 KB → ~27 KB** | 10× выигрыш |
| SSL-сертификат | н/д | **self-signed в цепочке** | требует `-k` / `verify=False` |

> Прямое сравнение скоростей рендеринга в этот раз невозможно: не было авторизованной сессии.
> Baseline (81–151 с) остаётся единственным замером с auth. Повторить с auth-сессией пользователя.

---

## Новые находки (главное)

1. **ESIA/Госуслуги auth — не базовая HTTP Auth.** Логин перенаправляет на `esia.gosuslugi.ru` с OAuth2. Playwright с реальным браузером обязателен — никакой замены через curl+cookie нет.

2. **SSL: self-signed certificate in chain.** `curl` по умолчанию отказывает. Все HTTP-клиенты в коде должны иметь `verify=False` / `ssl_verify=False`. Это текущее состояние — может измениться.

3. **GZIP: 10× compression ratio.** `list_region1_perpage50.html` (268 KB) → gzip 27 KB. `detail_lot_9990.html` (32 KB) → gzip 6 KB. `Accept-Encoding: gzip` нужно всегда слать — экономит трафик и ускоряет download-фазу.

4. **Sort `-DATE_CREATE` (DESC) подтверждён в fixture.** Ссылки в HTML: `sort=DATE_CREATE` (ASC) и `sort=-DATE_CREATE` (DESC, стандарт Yii2). Аналогично `sort=-DATE_UPDATE`. Это основа early-exit стратегии: page 1 с `-DATE_CREATE` → newest lots first.

5. **`DATE_UPDATE` sort — дополнительная возможность.** Позволяет находить лоты, у которых изменился статус (`Свободен` → `Резерват` и обратно). Если нужно мониторить "освободившиеся" лоты — `sort=-DATE_UPDATE` + фильтр `freeLotStatus=Свободен`.

6. **Export endpoint обнаружен:** `POST /cabinet/start-export-freelot` → `/export-file/check-export` (polling). Работает через очередь (асинхронно). Без auth: 500/401. **Если авторизоваться** — потенциально CSV/Excel выгрузка всех лотов региона без HTML-парсинга. Требует отдельного исследования с сессией.

7. **Public AJAX endpoint без auth:** `GET /default/get-free-lot-land-category-multiselect-options` → 200, 110 KB HTML-опций категорий земель. Аналогично `/default/get-territory-multiselect-options`. Эти endpoints доступны анонимно — можно использовать для заполнения справочников без сессии.

8. **Pagination: 21 страница × 50 = ~1050 лотов в регионе 1** (на момент fixture). Полный обход = 21 запрос. С early-exit (sort=-DATE_CREATE) типичный цикл = 1–2 запроса.

9. **`freeLotStatus` — поле статуса участка.** Значения видны в fixture: `Свободен`, `Резерват` (возможно другие). Фильтрация по статусу через `FreeLotSearch[freeLotStatus]` должна работать, но поля нет в HTML-форме — только в data-allowed-columns. Возможно GET-параметр работает напрямую.

10. **Сервер IP: 217.77.104.196** — один IP, нет CDN/балансировщика. Таймаут из предыдущего теста подтверждает: один бэкенд, единая точка отказа.

---

## Детальные измерения

### Запросы без auth (анонимные)

| # | URL | Status | Time | Size | Заметки |
|---|---|---|---|---|---|
| 1 | `/robots.txt` | 200 | 0.31 с | 22 B | `Allow: /` — никаких запретов crawl |
| 2 | `/sitemap.xml` | 404 | 1.08 с | ~25 KB | Нет sitemap, отдаёт кастомную 404 |
| 3 | HEAD `/cabinet/free-lot?region=1&per-page=20` | 302 | 0.38 с | 0 | → `/default/login` |
| 4 | HEAD `/export-file/check-export` | 401 | 1.08 с | 0 | `user is required` |
| 5 | HEAD `/cabinet/start-export-freelot` | 500 | 1.13 с | 0 | Internal Error без сессии |
| 6 | HEAD `/default/get-territory-multiselect-options` | 200 | 0.29 с | 0 | Публичный AJAX |
| 7 | GET `/default/get-territory-multiselect-options` | 200 | 0.29 с | 0 | Пустой ответ без params |
| 8 | HEAD `/default/login` | 302 | 0.56 с | 0 | → ESIA/Госуслуги OAuth2 |
| 11 | HEAD `/` | 200 | 1.32 с | 0 | Публичная главная |
| 12 | GET `/` | 200 | 2.01 с | 548 KB | Публичная главная |
| 13 | HEAD `/cabinet/free-lot-view?id=9990` | 302 | 0.38 с | 0 | → `/default/login` |
| 14 | HEAD `/free-lot?region=1` | 404 | 1.05 с | 0 | Нет публичного роута |
| 15 | HEAD `/cabinet/ogv-filter-for-citizen` | 302 | 0.37 с | 0 | → login |
| 18 | GET `/default/maininfo` | 200 | 1.18 с | 217 KB | Публичная страница "О регионах" |
| 19 | GET `/default/maininfo?format=json` | 200 | 1.15 с | 217 KB | format=json игнорируется |
| 22 | GET `/` с gzip | 200 | 1.45 с | **171 KB** | 548 KB → 171 KB (3.2× ratio) |
| 25 | HEAD `/news` | 200 | 1.38 с | 0 | Публичный |
| 28 | HEAD `/cabinet/free-lot-map` | 404 | 1.10 с | 0 | Нет map-endpoint |
| 29 | HEAD `/cabinet/free-lot/export` | 404 | 1.32 с | 0 | Нет export sub-route |
| 30 | HEAD `/cabinet/free-lot-history` | 404 | 1.10 с | 0 | Нет history-endpoint |
| 31 | GET `/cabinet/start-export-freelot` AJAX | 500 | 0.34 с | 66 B | "Возникла внутренняя ошибка" |
| 33 | GET `/export-file/check-export` AJAX | 401 | 0.26 с | 16 B | "user is required" (JSON!) |
| 37 | GET `/default/get-free-lot-land-category-multiselect-options` | 200 | 0.30 с | 110 KB | **Публичный, список категорий** |
| 38 | GET `/` с `If-Modified-Since` | 200 | 1.16 с | 0 | 304 не поддерживается |

### Detail page response time

Недоступна без auth → 302 за ~380 мс. Из fixture `detail_lot_9990.html`: 32 KB HTML (6 KB gzip).

### Cabinet redirect timing (3 подряд, интервал 3 с)

| Попытка | TIME_CONNECT | TTFB | Total |
|---|---|---|---|
| 1 | 23 мс | 327 мс | 327 мс |
| 2 | 23 мс | 343 мс | 343 мс |
| 3 | 23 мс | 346 мс | 347 мс |

Стабильно ~330–350 мс для redirect. Это overhead аутентификационного middleware PHP/Yii2 (проверка сессии → нет → redirect).

### Public page timing (3 подряд, интервал 3 с)

| Попытка | Total |
|---|---|
| 1 | 1.29 с |
| 2 | 4.37 с (spike!) |
| 3 | 1.18 с |

Сервер нестабилен: spike в 4.4 с — без auth. Это публичная страница, не тяжёлый cabinet. Вероятно: одна PHP-воркер была занята → пришлось ждать освобождения.

---

## Headers analysis

### Все страницы

```
Cache-Control: no-store, no-cache, must-revalidate, post-check=0, pre-check=0
Pragma: no-cache
Expires: Thu, 19 Nov 1981 08:52:00 GMT
Vary: Accept-Encoding
X-Frame-Options: SAMEORIGIN
X-XSS-Protection: 1; mode=block
X-Content-Type-Options: nosniff
```

**Нет:** `ETag`, `Last-Modified`, `Server`, `X-Powered-By`.

**Выводы:**
- Conditional GET (If-Modified-Since / If-None-Match) **не работает** — сервер всегда 200.
- GZIP поддерживается (`Vary: Accept-Encoding`) — использовать обязательно.
- Set-Cookie при каждом запросе: `PHPSESSID`, `_csrf`, `browser_info`, `session-cookie`.
- SSL: self-signed certificate → `-k` / `ssl_verify=False` обязательно.

---

## Альтернативные endpoints

### Что работает публично (без auth)

| Endpoint | Метод | Статус | Результат |
|---|---|---|---|
| `/default/get-free-lot-land-category-multiselect-options` | GET | 200 | 110 KB HTML `<option>` список |
| `/default/get-territory-multiselect-options?type=municipal&parentId=N` | GET | 200 | HTML `<option>` по региону |
| `/feedback/send` | POST | ? | Форма обратной связи |

### PJAX

Технически поддерживается: в fixture есть `#free-lots-pjax-container`, JS: `jQuery(document).pjax(...)` с timeout 300 с. Сервер получает `X-PJAX: true` + `X-PJAX-Container: #free-lots-pjax-container` — и должен вернуть только содержимое контейнера вместо полного HTML.

**НЕ протестировано с auth** — без сессии сервер всегда 302. Если PJAX работает, response будет ~30–50 KB вместо 268 KB. **Высокий приоритет для тестирования с реальной сессией.**

### JSON API

`?format=json` на публичных страницах игнорируется (возвращает HTML). Специализированных JSON endpoints не обнаружено. `/export-file/check-export` возвращает JSON (`"user is required"` — 16 B) — значит при auth вернёт JSON-статус экспорта.

### Export

Двухфазный:
1. `POST /cabinet/start-export-freelot` с CSRF-токеном → запускает async экспорт, возвращает task ID
2. `GET /export-file/check-export?...` с polling interval=1000 мс → когда готово, ссылка на файл

Формат файла неизвестен (предположительно XLSX/CSV по `export-checker.css`). **Если файл = CSV, это обходит весь HTML-парсинг.**

---

## Анонимный доступ

**Результат: нет.** Все `/cabinet/*` endpoints → 302 → `/default/login` → 302 → `esia.gosuslugi.ru` OAuth2.

Процесс логина:
1. GET `/default/login` → 302 → ESIA OAuth2 с `client_id=ROREESTR03`
2. Пользователь логинится через Госуслуги
3. ESIA redirects back с `code=...`
4. Сайт обменивает code на токен → создаёт сессию

**Следствие:** Playwright обязателен для получения и поддержания сессии. Cookie-based сессия (`PHPSESSID` + `session-cookie`) живёт `Max-Age=86400` (24 ч) — значит логин нужен раз в сутки.

---

## Архитектурные импликации

### 1. `Accept-Encoding: gzip` — добавить везде (высокий приоритет)

Экономия: 268 KB → 27 KB (10×) для list-страницы, 32 KB → 6 KB для detail. При скорости рендеринга 80–150 с это несущественно (download-time мал), но при нормальной скорости (5–30 с) экономия трафика значима. httpx в Python делает gzip автоматически.

### 2. Sort `-DATE_CREATE` → early-exit (высокий приоритет)

URL: `/cabinet/free-lot?region=1&sort=-DATE_CREATE&per-page=50`

Стратегия: запросить страницу 1, проверить ID лотов vs последний известный. Если все ID ≤ max_known_id → новых нет → стоп. Типичный цикл = **1 запрос** вместо 21.

Аналог для мониторинга статусов: `sort=-DATE_UPDATE`.

### 3. SSL verify=False — обязательно

Все `requests.get(..., verify=False)` и httpx `ssl_verify=False`. Сервер использует self-signed cert.

### 4. Export endpoint — исследовать с auth (средний приоритет)

Если `POST /cabinet/start-export-freelot` → CSV/Excel — это кардинально меняет стратегию: один export-запрос раз в N минут вместо HTML-парсинга страниц.

### 5. PJAX — исследовать с auth (средний приоритет)

Если PJAX возвращает только `<tbody>` вместо 268 KB HTML, скорость парсинга и transfer size улучшатся. Требует тест с реальной сессией.

### 6. Session lifetime 24h → ежедневный re-login

`session-cookie: Max-Age=86400`. Реализация: при 302 → `/default/login` запускать Playwright re-login flow.

### 7. Conditional GET — НЕ реализовывать

Сервер не отдаёт ETag / Last-Modified. `Cache-Control: no-store` исключает 304-оптимизацию.

### 8. freeLotStatus filter — потенциальная оптимизация

`FreeLotSearch[freeLotStatus]=Свободен` в GET-параметрах может отфильтровать лоты в резервации. Если сервер поддерживает — можно уменьшить объём результатов. Требует проверки с auth.

---

## Этика / ограничения

- **Budget**: 40 запросов (лимит соблюдён)
- **Интервал**: 3 с между запросами
- **503/blacklist**: не получили — сервер стабильно отвечал
- **Без auth**: невозможно протестировать cabinet endpoints — все гипотезы о скорости рендеринга остались непроверенными
- **Геолокация**: IP вне РФ (217.77.104.196 — российский сервер). Возможна асимметрия задержек. Для финального теста нужен пользовательский ПК в РФ.

---

## Связи

- [[server-performance]] — baseline (12.05.2026, с auth)
- [[architecture/03-protocols#HttpClient]] — место для `verify=False` и `Accept-Encoding: gzip`
- [[decisions-log]] — ADR по интервалу мониторинга
