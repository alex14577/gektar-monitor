# ADR-024: TargetConfig + TorgiUrlBuilder — config seam for real target URL

**Context.** До этой ADR все три сервиса (`MonitorCycleService`, `FullScanService`, `EnrichmentService`) хардкодили `https://torgi.gov.ru/new/public/lots/search` и `https://torgi.gov.ru/new/public/lots/lot/{lot_id}` — URL **совершенно другой системы (госзакупки)**, не имеющей отношения к программе «Дальневосточный гектар». Реальный target — `https://надальнийвосток.рф` (Punycode: `xn--80aaggvgieoeoa2bo7l.xn--p1ai`). Это был latent production bug: сервис не мог скачать ни одной страницы реального сайта с момента запуска. Параллельно требовался config-seam для локального staging (fake-site) без пересборки.

Дополнительно: default SMTP host (`smtp.yandex.ru`) жил в `domain/models.py` `EmailConfig` — нарушение ADR-020 (SMTP SSOT = state.db): domain-модель не должна знать о конкретном SMTP-провайдере.

**Decision.**

1. **`TargetConfig`** — новый Pydantic sub-model в `Settings`. Поля: `base_url` (default = Punycode домен), `request_timeout_seconds` (default = 90, study: 80-150s/page), `user_agent`. Validator: `base_url` должен начинаться с `http://` или `https://`, trailing slash стрипается (idempotency).

2. **`TorgiUrlBuilder`** — frozen dataclass в `infra/http/url_builder.py`. Принимает `base_url` из `TargetConfig`. Endpoint paths (`/cabinet/free-lot`, `/cabinet/free-lot-view`) — модуль-level константы внутри builder-а, **не конфиг**: если сайт меняет path → меняется парсер; единица изменения одна. Квадратные скобки в query percent-encoded (`%5B`, `%5D`) для безопасного включения в GET URL без разбора на стороне `requests`.

3. **Env overrides** (`FIS_TARGET__*`) — `WatchdogConfigSource._apply_env_overrides()` применяет env-переменные поверх file-loaded Settings. Без `pydantic-settings` зависимости (30 строк, manual `os.environ`).

4. **SMTP defaults** → `infra/smtp/constants.py` (`DEFAULT_SMTP_HOST`, `DEFAULT_SMTP_PORT`). `EmailConfig.smtp_host` и `EmailNotifierConfig.smtp_host` → `str | None = None`. Domain-модель больше не знает о `smtp.yandex.ru`.

5. **Composition**: `TorgiUrlBuilder` собирается в `build_container()` из `config_source.current().target.base_url` и инжектится во все три сервиса. `_TORGI_ALLOWED_HOSTS` → Punycode форма (`xn--80aaggvgieoeoa2bo7l.xn--p1ai`) + unicode alias.

**Rationale.**

- `base_url` в `Settings` (file/env), endpoint paths — в коде: правильное разделение ответственностей. URL структура сайта — доменное знание парсера, не пользовательская конфигурация.
- Frozen dataclass для `TorgiUrlBuilder` — value-object, YAGNI протокол. DI через kw-only в конструкторе сервисов → trivial unit tests (подменить `TorgiUrlBuilder(base_url="http://localhost:8765")`).
- `FIS_TARGET__BASE_URL` env override — staging запускается без изменения `config.json`: `FIS_TARGET__BASE_URL=http://localhost:8765 python -m fis_monitor`.

**Alternatives rejected.**

- **`state.db` для `base_url`** — отвергнуто: ADR-020 SSOT применяется к SMTP credentials, не к infra endpoint-ам. `state.db` читается после Container init; `base_url` нужен при сборке.
- **`pydantic-settings` `BaseSettings`** — отвергнуто: extra runtime dep для 30-строчной задачи; `BaseModel` + manual env-merge достаточно.
- **`FisHttpClient` обёртка над `HttpClient`** — отвергнуто: нарушает Protocol-generic vs site-specific разделение; `HttpClient` Protocol должен оставаться site-agnostic.
- **`requests` `params=` dict вместо URL-template** — отложено: текущий `HttpClient` Protocol принимает полный URL строкой; рефакторинг Protocol — отдельный scope.

**Consequences.**

- Сервис после wire-up реально скачивает страницы `надальнийвосток.рф`.
- Staging через `FIS_TARGET__BASE_URL=http://localhost:8765` (для Wave 11b fake-torgi).
- Smoke-тесты могут задать custom `base_url` без перекомпиляции.
- Hot-reload `TorgiUrlBuilder` на reload `Settings` — out of scope (future work); builder пересоздаётся только на рестарт (restart = pересборка Container).
- Migration: существующие `config.json` не содержат `target.*` ключей — поведение совпадает с default (real domain). Backward-compat через pydantic `extra="forbid"` с `default_factory=TargetConfig`.
- `migrations_v1_to_v2.py` — frozen, `smtp.yandex.ru` в нём остаётся (applied migration).
