# ADR-017: Secrets handling — SecretStr + crash-dump exclusion

**Context.** `SmtpCredentials.password: str` риск утечки через `__repr__` в crash-логах. Diagnostic.zip мог зацепить `*.dmp`/`core.*`/`Werfault*`/`CrashDumps/` с фрагментами адресного пространства.

**Decision.**
- `SmtpCredentials.password: pydantic.SecretStr`. `__repr__`/`__str__` → `'***'`. Получить plain — только через `.get_secret_value()`.
- `DiagnosticsService` exclude-list расширен: `*.dmp`, `core.*`, `Werfault*`, `CrashDumps/`. Дополняет [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]].

**Consequences.** Двойной контур защиты secrets (логи + crash-dumps). Никакого overhead в runtime (SecretStr — wrapper).

**Расширение (gektar_monitor-ctz): pickle/deepcopy hard-block.**
`SmtpCredentials.__reduce__` / `__getstate__` / `__setstate__` / `__deepcopy__` переопределены и бросают `TypeError` с явной ссылкой на ADR-017. Причина: `SecretStr.__reduce__` preserves plaintext для round-trip — это design pydantic, но дыра для `pickle.dumps`, `multiprocessing.Queue`, `copy.deepcopy`. Заблокированы все пути на уровне domain-модели; `copy.copy` (shallow) разрешён — не пересекает границу сериализации. `faulthandler.dump_traceback` не блокируется из Python — OS-уровень (seccomp/AppArmor/SELinux) или exclude-list ADR-012.

См. также: [[decisions-log]], [[architecture/10-7-diagnostic-zip]], [[data-model/notifications]].
