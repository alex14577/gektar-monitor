# ADR-018: Onboarding FSM server-enforced

**Context.** Первая версия onboarding-gate редиректила на `?step=N` из query-param. Пользователь мог `GET /?step=4` и пропустить настройку SMTP.

**Decision.** Server-side state-machine с явными states, transitions, guards. `OnboardingService.advance(from_state, to_state)` валидирует переход. Middleware редиректит на **последний валидный step** (читая из БД), не на query-param. Полная спецификация — [[onboarding]].

States: `not_started → regions_set → smtp_configured → recipients_set → completed`. Guards включают `len(regions) > 0`, `smtp_test.last_result.ok OR email_skipped`, `len(recipients) > 0 OR email_skipped`, `test_email_sent OR email_skipped`.

**Consequences.** Невозможно пропустить шаг. Цена: state в БД (key `onboarding_state`), `OnboardingService.advance()` — атомарная операция через BEGIN IMMEDIATE. UI читает текущий state и редерит соответствующий step.

**Known limitation R3-M10 (`smtp_test_last_result_ok` подделывается через direct DB-write).** В trust-model MVP (single-user, доверенная локальная среда, ACL на `%LOCALAPPDATA%`) — приемлемо. Future hardening (не MVP): `HMAC(state_secret, host+port+user+timestamp)` записывается рядом с флагом, OnboardingService.advance проверяет HMAC. Roadmap-TODO.

См. также: [[decisions-log]], [[onboarding]].
