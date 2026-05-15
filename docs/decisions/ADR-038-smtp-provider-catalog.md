# ADR-038: SMTP provider catalog — упрощение ввода credentials по домену email

**Status:** Accepted (2026-05-15)

## Context

Текущий wizard ([[onboarding]] step 2) и `/settings` form требуют от пользователя ввести **четыре** SMTP-параметра вручную: `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`. Для 95% пользовательских доменов (`yandex.ru`, `mail.ru`, `gmail.com`, `outlook.com`, `icloud.com`, `rambler.ru`) эти host/port — **константа**, известная провайдеру. Пользователь не должен знать, что `mail.ru` → `smtp.mail.ru:465 implicit-TLS`, а `gmail.com` → `smtp.google.com:587 STARTTLS`.

Дополнительная проблема — major-провайдеры (Gmail, Outlook, Yahoo) **блокируют login обычным паролем** для third-party SMTP-клиентов. Пользователю нужен **app-password** из настроек безопасности аккаунта, иначе `smtplib.SMTPAuthenticationError` без объяснения причины. Сейчас UI этого не подсказывает.

Цель: пользователь вводит **только** `email-login` + `password`. Если домен в каталоге — host/port подставляются автоматически (read-only в UI, override через advanced). Если домен неизвестен — fallback на текущий manual-ввод.

Изменения **только UI/web-слой**: схема `SmtpCredentials` (БД, [[data-model/settings]]) не меняется, миграция не нужна.

## Decision

### 1. Catalog как infra-Protocol, не domain-knowledge

Логика «по домену → endpoint» — **не domain invariant** (не бизнес-правило ФИС-44/торгов), а **infra knowledge** о внешних SMTP-провайдерах. По аналогии с [[decisions/ADR-015-smtp-host-validation|ADR-015]] (host policy в infra, не в `SmtpCredentials`-валидаторе) и [[decisions/ADR-022-allowed-tracked-fields-ssot-smtp-policy-error|ADR-022]] (SSOT принципы) — каталог живёт в `infra/smtp/`.

Protocol-шов в `domain/interfaces.py`:

```python
@runtime_checkable
class SmtpProviderCatalog(Protocol):
    def lookup(self, email: str) -> ProviderSuggestion | None: ...
```

`ProviderSuggestion` — frozen dataclass в `domain/models.py` (DTO без поведения, как `ResolvedSmtpEndpoint`):

```python
@dataclass(frozen=True, slots=True)
class ProviderSuggestion:
    smtp_host: str          # e.g. "smtp.yandex.ru"
    smtp_port: int          # 465 or 587
    use_starttls: bool      # True for :587 STARTTLS, False for :465 implicit TLS
    app_password_url: str | None  # документация провайдера, None если обычный пароль ок
    provider_label: str     # "Yandex", "Gmail" — для UI display
```

Реализация `StaticSmtpProviderCatalog` в `infra/smtp/provider_catalog.py` — hardcoded dict, без I/O, чистая функция.

### 2. Источник truth — hardcoded dict в коде

Каталог провайдеров — **код, не data-файл и не DNS**:

| Domain alias                 | host                  | port | TLS         | app-password required |
|------------------------------|-----------------------|------|-------------|----------------------|
| yandex.ru / yandex.com / ya.ru | smtp.yandex.ru     | 465  | implicit    | yes (link в `app_password_url`) |
| mail.ru / list.ru / inbox.ru / bk.ru | smtp.mail.ru | 465  | implicit    | yes |
| rambler.ru / lenta.ru / autorambler.ru | smtp.rambler.ru | 465 | implicit | no |
| gmail.com / googlemail.com   | smtp.gmail.com        | 587  | STARTTLS    | yes |
| outlook.com / hotmail.com / live.com / msn.com | smtp.office365.com | 587 | STARTTLS | yes |
| icloud.com / me.com / mac.com | smtp.mail.me.com    | 587  | STARTTLS    | yes |
| yahoo.com / yahoo.ru / ymail.com | smtp.mail.yahoo.com | 465 | implicit | yes |

Точный набор/линки финализирует writer при имплементации, но **структура и semantics — выше**.

### 3. Web endpoint — `GET /settings/smtp/suggest?email=...`

JSON-эндпоинт в существующем `web/routes/settings.py` router (single SSOT для SMTP-настроек). HTMX/fetch с debounce 300ms на blur/input email-поля. Ответ:

```json
{"smtp_host": "smtp.yandex.ru", "smtp_port": 465, "use_starttls": false,
 "app_password_url": "https://yandex.ru/support/...", "provider_label": "Yandex"}
```

Если домен не в каталоге — `200 OK` + `{"smtp_host": null, ...}` (НЕ 404 — это валидный ответ «нет suggestion, покажи advanced»). UI скрывает host/port под "Advanced settings" disclosure если suggestion есть; разворачивает если null.

CSRF не нужен — это GET без изменения state. Rate-limit не нужен — функция чисто in-memory lookup без I/O.

### 4. Server-side double-check (defence in depth)

UI prefills host/port из `/suggest`, но пользователь может **переопределить** (поменять host в advanced-секции). На сабмите формы `POST /settings/smtp` валидация **не зависит от suggestion**:

1. Pydantic `SmtpCredentials` валидация (формат, [[decisions/ADR-015-smtp-host-validation|ADR-015]] format-validator)
2. `DefaultSmtpHostPolicy.resolve_and_check(smtp_host, smtp_port)` — fail-closed на любом запрещённом IP

Critical: **suggestion НЕ обходит host-policy.** Если кто-то добавил в каталог `localhost` — `_reject_pre_resolve` его срубит. Catalog — UX-помощник, безопасность — у policy.

### 5. Fail-open для unknown domain — это feature, не bug

Если email на корпоративном или редком домене (`mycompany.ru`) — `suggest` вернёт `null`, UI покажет manual-ввод. Это **не ошибка**, это deliberate fallback. Никаких MX-lookup / DNS-SRV / autoconfig-XML — каталог детерминирован, без сетевых вызовов.

### 6. App-password hint в UI

Если `app_password_url != null` — UI рисует под полем "Password" hint-блок:

> Gmail требует **app-password** (не обычный пароль аккаунта). [Создать app-password →](url)

Без bypass-логики (нельзя кликнуть "сохранить как обычный пароль" в обход хинта) — хинт чисто информативный, провайдер всё равно отбросит regular password.

## Consequences

### Positive

- **UX**: 4 поля → 2 поля в типовом случае (login + password). Время первого setup'а падает.
- **Меньше ошибок**: пользователь не пишет `smtp.yandex.com` вместо `smtp.yandex.ru`, не путает порты 465/587/25.
- **Security без compromise**: catalog лишь suggestion'ит, validate-pipeline ([[decisions/ADR-015-smtp-host-validation|ADR-015]]) — нетронут.
- **Detectable misconfiguration**: app-password hint объясняет частую причину `SMTPAuthenticationError` для Gmail/Outlook.
- **Нет миграции БД**: схема `smtp_credentials` не меняется ([[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]]).

### Negative / Risks

- **Catalog drift**: если Yandex сменит SMTP-host (маловероятно, но возможно) — нужен code-update + релиз. Risk acceptable: SMTP-эндпоинты крупных провайдеров меняются раз в десятилетие; релизный цикл проекта это покрывает.
- **Mixed-case domains**: `User@Yandex.RU` — нужна нормализация (lowercase, strip whitespace) перед lookup. Writer обязан покрыть тестом.
- **IDN-домены**: `почта@яндекс.рф` — punycode (`xn--...`). Catalog ключи — punycode-формы; нормализация через `email.utils` / `idna`. Edge case, маловероятен на бизнес-инсталляции.

### Reversibility

Очень высокая. Удаление catalog → fallback в текущее UI поведение (manual host/port). Никаких persistence-следов (`smtp_host`/`smtp_port` уже в БД, просто были prefilled).

## Alternatives considered

### A. Catalog как config-файл (`catalog.json` в `static/` или `data_dir`)

**Отвергнуто.** Преимущество — hot-update без релиза. Цена: ещё один источник truth, который надо валидировать, версионировать, переживать через `diagnostic.zip` allowlist ([[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]]) и crash-dump exclusion. Hardcoded dict — версионируется через git, тестируется юнит-тестом, не требует runtime-валидации.

### B. DNS-based autoconfig (RFC 6186 `_submission._tcp.<domain>` SRV-record)

**Отвергнуто.** Стандарт существует, но on-prem поддержка hit-or-miss (Gmail публикует, Mail.ru — нет). Зависимость от DNS — лишняя точка отказа, плюс новый attack surface (DNS-rebinding suggestion'а — атакующий может подсунуть `smtp-evil.example.com:25`). Даже если фильтровать через host-policy, добавление сетевого вызова в UX-helper — overkill.

### C. Mozilla ISPDB / Thunderbird autoconfig XML

**Отвергнуто.** Внешний HTTP-сервис, supply-chain risk, требует TLS + ([[decisions/ADR-037-tls-russian-trusted-ca-bundle|ADR-037]]) bundle, неясная availability на российских сетях. Hardcoded dict покрывает 95% пользователей при 0 сетевых зависимостях.

### D. Логика в domain (`SmtpCredentials.suggest_for(email)` classmethod)

**Отвергнуто.** Нарушает разделение domain vs infra из [[decisions/ADR-015-smtp-host-validation|ADR-015]]: знание о конкретных SMTP-хостах провайдеров — infra-detail, не бизнес-инвариант. Domain должен оставаться чистым (без знаний о Yandex/Gmail/Mail.ru как сущностях).

### E. POST `/settings/smtp/suggest` вместо GET

**Отвергнуто.** Lookup — идемпотентен, без побочных эффектов, без секретов в URL (email — публичный identifier на этапе onboarding'а). GET корректен по REST-семантике, кэшируется браузером.

### F. Inline suggestion (без отдельного endpoint, через прямой render шаблона)

**Отвергнуто.** Требовал бы full-form re-render на каждое изменение email-поля. Отдельный JSON-endpoint + JS prefill — стандартный паттерн HTMX в проекте, согласован с [[decisions/ADR-029-vendor-htmx-no-cdn|ADR-029]].

## Implementation notes

### Новые файлы

- `src/fis_monitor/infra/smtp/provider_catalog.py` — `StaticSmtpProviderCatalog` + module-level dict `_CATALOG`
- `tests/infra/smtp/test_provider_catalog.py` — unit-тесты lookup (известный / неизвестный / mixed-case / IDN / пустая строка / без `@`)
- `tests/unit/web/routes/test_settings_smtp_suggest.py` — endpoint-тест (200 с данными, 200 с null, 400 на malformed email)

### Изменённые файлы

- `src/fis_monitor/domain/models.py` — добавить `ProviderSuggestion` dataclass
- `src/fis_monitor/domain/interfaces.py` — добавить `SmtpProviderCatalog` Protocol
- `src/fis_monitor/composition/` (Container/wiring) — register `StaticSmtpProviderCatalog` как `SmtpProviderCatalog`
- `src/fis_monitor/web/routes/settings.py` — handler `GET /settings/smtp/suggest`
- `src/fis_monitor/web/deps.py` — DI provider для `SmtpProviderCatalog`
- `src/fis_monitor/web/templates/onboarding/_step2.html.jinja` — JS debounced fetch на blur email, prefill host/port, advanced disclosure, app-password hint render
- `src/fis_monitor/web/templates/settings/smtp_form.html.jinja` (если есть) — то же
- `tests/unit/web/routes/test_onboarding_save.py` — флаг что save с auto-suggested host работает (без regression'а на host-policy)

### Тесты по слоям (см. [[architecture/09-test-strategy]])

- **Layer 1 (domain)** — `ProviderSuggestion` это frozen dataclass без поведения; **тест не нужен** (per testing policy: не покрывать тривиальные DTO).
- **Layer 2 (infra)** — `StaticSmtpProviderCatalog.lookup`:
  - известный домен → точные поля
  - неизвестный домен → `None`
  - mixed-case (`USER@YANDEX.RU`) → tot же результат что lowercase
  - email без `@` или пустой → `None` (НЕ raise)
  - все ключи каталога имеют валидные host (проходят `DefaultSmtpHostPolicy._reject_pre_resolve`)  ← **критичный contract-тест**
- **Layer 4 (web)** — `/settings/smtp/suggest`:
  - `?email=user@yandex.ru` → JSON с host/port
  - `?email=user@unknown.example` → JSON со всеми null + provider_label null
  - `?email=` или невалидный — `400`
  - response не содержит секретов (паролей нигде нет в данном пути, но проверить что не вернули случайно лишних полей)
- **Layer 5 (smoke)** — не нужен (UI-prefill, не критичный flow).

### Ratio LOC

Ожидание: catalog implementation ~50 LOC, tests ~80 LOC (1.5:1 — норма для адаптера). Если writer выдаст >2:1 — параметризовать кейсы.

### Что НЕ делать

- НЕ менять `SmtpCredentials` Pydantic-схему.
- НЕ добавлять миграцию SQLite.
- НЕ менять `EmailNotifier` / `DefaultSmtpHostPolicy` / `email_notifier.py` — поведение runtime'а уведомлений не затрагивается.
- НЕ кэшировать ответы `/suggest` на сервере — lookup стоит копейки, кэш = лишняя сложность.
- НЕ делать MX-lookup, SRV-record fallback, ISPDB-fetch (см. Alternatives B/C).

См. также: [[decisions-log]], [[decisions/ADR-015-smtp-host-validation|ADR-015]], [[decisions/ADR-020-smtp-host-port-ssot-state-db|ADR-020]], [[decisions/ADR-021-manual-starttls-connect-by-ip|ADR-021]], [[data-model/settings]], [[onboarding]].
