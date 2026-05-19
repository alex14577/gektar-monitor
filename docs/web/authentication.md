# Авторизация

## Схема
Сайт авторизует пользователей **только через ЕСИА (Госуслуги)** по OAuth2.

```
Пользователь → /cabinet/* (нет сессии)
            → 302 /default/login
            → 302 https://esia.gosuslugi.ru/aas/oauth2/ac?client_id=ROREESTR03&...
            ← после логина: redirect_uri=/esia/auth?code=...&state=...
            → сайт обменивает code на токен, заводит PHPSESSID-сессию
            → редирект в кабинет
```

### Параметры OAuth2 (из `Location` заголовка):
- `client_id=ROREESTR03` — клиент Росреестра
- `scope` (что запрашивается у пользователя):
  `fullname birthdate gender snils inn id_doc birth_cert_doc email mobile contacts usr_org kid_fullname kid_birthdate kid_gender kid_snils kid_inn kid_birth_cert_doc`

Это **большой объём ПДн**, включая данные детей. Пользователь даёт согласие при первом входе.

## ESIA cookies (на стороне gosuslugi.ru, не у нас)
- `ESIA_SESSION`, `bs` — Max-Age=10800 (3 часа)
- `JSESSIONID` — Path=/aas/oauth2

## Cookies на стороне ФИС, нужные для авторизованного запроса
Минимально достаточный набор (проверено):
- `PHPSESSID`
- `JSESSIONID` (важно! без него — 302 на login)
- `_csrf`
- `session-cookie`

При наличии этого набора `/cabinet/profile` → 200, `/cabinet/free-lot?region=1` → 200.

### Важное наблюдение
PHPSESSID + _csrf без JSESSIONID **не достаточно** — сервер всё равно редиректит на ЕСИА. JSESSIONID, судя по всему, выдаётся отдельным Java-компонентом, который и хранит факт «пользователь авторизован через ЕСИА».

## TTL рабочей сессии
По умолчанию `Max-Age` у `session-cookie` = 86400s (сутки), у PHPSESSID = Session (до закрытия браузера). На практике сессия живёт **до ESIA_SESSION** = 3 часа без активности. Если на ЕСИА была галка «Запомнить меня» — refresh-токен продлевает её до 30 дней.

## UX flow — кнопка входа в мониторе

Кнопки «Войти через Госуслуги» (modal в `base.html.jinja`) и «Войти заново» (banner в `feed.html.jinja`) реализованы как `<button type="button" data-action="login-start">` — без `href`. JS-обработчик в [[glossary#auth.js|`auth.js`]] ловит клики через event delegation и выполняет:

```
1. fetch POST /auth/start
   → 202  → запустить polling GET /auth/status каждые 2 с (hard timeout 5 мин)
   → 409  → job уже идёт → перейти в polling (join existing job)
   → 429  → toast «Попробуйте через минуту», disable кнопки на 60 с
   → 503  → toast «Сервис запускается»

2. polling GET /auth/status:
   while running == true → ждать
   when running == false:
     last_outcome.success == true  → toast «Вход выполнен» + window.location.reload()
     last_outcome.success == false → toast с error-mapping + re-enable button
       error «timeout»     → «Время вышло»
       error «cancelled»   → «Отменено»
       error «playwright:*» → «Ошибка браузера»
```

Variant B (bd gektar_monitor-oem): GET /auth/login роут не создавался — все действия через JS-fetch к существующим POST /auth/start и GET /auth/status. CSRF: POST проходит через `CsrfHostOriginMiddleware` автоматически (Origin header отправляется браузером с каждым fetch).

## Безопасное обращение с cookies
- Cookies дают **доступ к ЛК пользователя в ФИС** (можно читать персональные данные, статусы заявлений, документы).
- НЕ дают прямой доступ к Госуслугам — но через них можно подать заявление **от имени пользователя** на этом сайте. Поэтому скрипт должен делать **только GET-запросы**, никаких POST.
- На стороне клиента (на его ПК) cookies хранятся в стандартном профиле Chromium/Playwright — это безопасно.
- Передача cookies через интернет (нам в чат) = временная компрометация. После отладки пользователь должен разлогиниться, чтобы инвалидировать сессию.

См. также: [[product/risks-legal]], [[product/monitoring-plan]].
