---
id: ADR-047
title: TLS trust posture for Playwright login context — accepted residual risk
status: accepted
date: 2026-05-17
related:
  - "[[ADR-011-dns-rebinding-host-allowlist]]"
  - "[[ADR-026-distribution-packaging-pyinstaller]]"
  - "[[ADR-027-silent-cookie-refresh]]"
  - "[[ADR-034-cookie-bridge-playwright-requests]]"
---

# ADR-047 — TLS trust posture for Playwright login context

## Context

`src/fis_monitor/infra/playwright/login.py` запускает Chromium через
`launch_persistent_context(headless=False, ignore_https_errors=True)` для
взаимодействия с gosuslugi.ru / esia.gosuslugi.ru / надальнийвосток.рф.
Флаг необходим: эти сайты обслуживаются сертификатами от **Russian Trusted
Root CA (Минцифры)**, которого нет в дефолтном trust store Chromium. Без
`ignore_https_errors=True` любой `page.goto()` падает с
`ERR_CERT_AUTHORITY_INVALID`.

Действующий контроль — `context.route("**/*", ...)` с allowlist'ом из
`composition.py:_TORGI_ALLOWED_HOSTS` (надальнийвосток.рф в двух
представлениях + `*.gosuslugi.ru`). Не-allowlist хосты аборчатся.

Предыдущий inline-комментарий утверждал, что whitelist «нейтрализует» risk
от `ignore_https_errors=True`, потому что non-whitelisted хосты аборчатся
«before any TLS happens». Это **некорректно**: Playwright `route()` реализован
поверх CDP `Fetch.enable`; перехват — на HTTP request stage, **после** TLS
handshake. То есть `ignore_https_errors` consult-ится раньше route
interception. Whitelist защищает только от exfil/SSRF (defense-in-depth),
но не аутентифицирует TLS-peer для **whitelisted**-хостов.

## Decision

**Accept the residual MITM risk.** `ignore_https_errors=True` остаётся.
Никаких изменений в коде не вносится сверх обновления вводящего в
заблуждение комментария в `login.py`.

Обоснование:

- Полноценный fix — забандлить Russian Trusted Root CA в Chromium profile
  NSS DB через `certutil` — оценён как **overengineering** для текущей
  стадии проекта: требует расширения distribution-pipeline ([[ADR-026]]),
  синхронизации с bd `zclo` (NSS bundling), отдельной интеграции для
  `requests` ([[ADR-034]]).
- Per-host scoping `ignore_https_errors` через Playwright API
  **невозможно**: ни `context.route`, ни `page.route` не предоставляют
  хук для повторной TLS-валидации.
- Целевая аудитория и сценарий (десктопный headed-login на доверенной
  сети) делают MITM-вектор низковероятным в практике.

## Threat model (для будущего ревью решения)

**Adversary**: same-LAN attacker (открытый wifi, скомпрометированный
домашний роутер), ISP-level transit attacker.

**Vulnerable surface** при `ignore_https_errors=True`:

- `esia.gosuslugi.ru` login form POST (телефон/СНИЛС + пароль).
- MFA / SMS / TOTP codes.
- Session cookies после успешного логина (продлеваются silent refresh,
  [[ADR-027-silent-cookie-refresh]]).

Whitelist **не помогает** против этого вектора — атакуемые хосты И ЕСТЬ
whitelist.

**Severity**: High при реализации. **Likelihood**: Low–Medium —
требует сетевого присутствия атакующего на пути пользователя.

## Alternatives considered

**(a) Bundle Минцифры root CA + drop `ignore_https_errors`** — отклонено
как overengineering для текущей стадии (см. Decision).

**(b) Scoped `ignore_https_errors` через preflight TLS check в route()** —
**технически невозможно**: Playwright API не предоставляет хук
manual-verify-then-continue.

**(c) Linux network namespace + iptables allowlist** — отклонено: грубо,
ломает DNS, не аутентифицирует peer.

## Consequences

- Security posture **явно задокументирована**, не скрыта за неверным
  inline-комментарием. Будущий ревьюер видит реальную картину и может
  принять решение пересмотреть, когда threat model изменится.
- Defense-in-depth host-whitelist сохраняется — блокирует exfil/SSRF на
  телеметрию/CDN.
- Этот ADR следует пересмотреть, если: (а) появятся отчёты о MITM в
  реальной эксплуатации, (б) Минцифры root CA попадёт в default Chromium
  trust store, (в) Playwright добавит per-host TLS scoping.

## Glossary

- **Russian Trusted Root CA (Минцифры)** — корневой УЦ Минцифры РФ; не
  входит в default trust store Chromium/Firefox.
- **CDP Fetch.enable** — Chrome DevTools Protocol domain для request
  interception; срабатывает post-TLS, pre-HTTP-bytes.
