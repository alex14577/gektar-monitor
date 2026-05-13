# ADR-017: Secrets handling — SecretStr + crash-dump exclusion

**Context.** `SmtpCredentials.password: str` риск утечки через `__repr__` в crash-логах. Diagnostic.zip мог зацепить `*.dmp`/`core.*`/`Werfault*`/`CrashDumps/` с фрагментами адресного пространства.

**Decision.**
- `SmtpCredentials.password: pydantic.SecretStr`. `__repr__`/`__str__` → `'***'`. Получить plain — только через `.get_secret_value()`.
- `DiagnosticsService` exclude-list расширен: `*.dmp`, `core.*`, `Werfault*`, `CrashDumps/`. Дополняет [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]].

**Consequences.** Двойной контур защиты secrets (логи + crash-dumps). Никакого overhead в runtime (SecretStr — wrapper).

См. также: [[decisions-log]], [[architecture/10-7-diagnostic-zip]], [[data-model/notifications]].
