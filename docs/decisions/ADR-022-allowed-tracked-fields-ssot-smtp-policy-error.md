# ADR-022: ALLOWED_TRACKED_FIELDS SSOT через typing.get_args + SmtpHostPolicyError наследует UpstreamError

**Context.** Два смежных кодовых решения из `domain/diff.py` и `domain/errors.py`, не покрытые явным ADR.

**Decision 1 — ALLOWED_TRACKED_FIELDS SSOT.**
`ALLOWED_TRACKED_FIELDS: frozenset[str] = frozenset(typing.get_args(TrackedField))` вместо ручного дублирования значений Literal. Альтернатива — держать два отдельных определения (Literal для типов, frozenset для runtime-проверки) — риск дрейфа при добавлении нового tracked-поля: тип обновлён, frozenset забыт → инъекция в SQL-identifier остаётся незакрытой.

**Decision 2 — SmtpHostPolicyError(UpstreamError).**
Ошибки DNS-resolve и blocklist при проверке SMTP-хоста классифицируются как `upstream` ([[decisions/ADR-003-error-strategy-exceptions-result-for-notifier|ADR-003]]: UpstreamError — для network/DNS/HTTP-слоя). Альтернатива — отдельная иерархия от `DomainError` — создаёт неоднородность: `SmtpEmailNotifier.send()` ловит и `UpstreamError`, и `SmtpHostPolicyError` разными `except`-ветками, нарушая принцип «один обработчик на тип сбоя».

**Consequences.** Нулевая возможность дрейфа между Literal и runtime-frozenset. `SmtpHostPolicyError` обрабатывается в `run_forever()` единым `except UpstreamError` блоком без дополнительных ветвлений.

См. также: [[decisions-log]], [[decisions/ADR-016-repository-invariants-begin-immediate|ADR-016]], [[data-model/errors]].
