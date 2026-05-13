# ADR-020: SMTP host/port SSOT = state.db (R4-C1)

**Context.** Первая версия notifications.md/data-model.md упоминала `smtp_host`/`smtp_port` в `EmailConfig` (config.json), а `smtp_user`/`smtp_password` — в state.db (`smtp_credentials`). Это создавало:
1. **Split SSOT** — два места хранения одного логического объекта (creds = host+port+user+password). Pydantic-валидация config.json и UPSERT в state.db — две разные tx, нет атомарности. Race window между UI «сохранить SMTP-настройки» и `SmtpEmailNotifier.send()`: новый host может подгрузиться раньше нового user → попытка login против чужого хоста.
2. **Config-write-vector** — `config.json` имеет write-API через `WatchdogConfigSource` reload (модификация файла на диске → reload). Атакующий с write-доступом к config.json (например, через misconfigured ACL) мог бы перенаправить SMTP на свой хост, не трогая `smtp_credentials` — далее жертва шлёт письма с своими creds на attacker.example.
3. **R4-C1 — schema.sql::smtp_credentials НЕ содержал smtp_host/smtp_port** — `SqliteSmtpCredentialsRepository.save()` физически некуда было писать эти поля. Блокер для кода.

**Decision.** **SSOT = state.db**. Расширить `smtp_credentials`:
```sql
ALTER TABLE smtp_credentials ADD COLUMN smtp_host TEXT NOT NULL;
ALTER TABLE smtp_credentials ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587
    CHECK (smtp_port BETWEEN 1 AND 65535);
```
`Pydantic SmtpCredentials` в domain получает поля `smtp_host: str` и `smtp_port: int`.

В `EmailConfig` (`config.json`) остаётся **только** `use_default_smtp: bool` (формальный признак). Литералы `smtp.yandex.ru:587` хранятся в коде (`infra/smtp/defaults.py`) — fallback при пустой таблице первой установки. Поля `smtp_host`/`smtp_port` в EmailConfig — **deprecated**, читаются только для миграции при первом запуске v2.

`SettingsService.set_smtp_credentials(creds)` пишет ВСЕ 4 поля (host, port, user, password) в одну BEGIN IMMEDIATE tx — атомарность гарантируется.

**Consequences.**
- Когезия: один логический объект — одна таблица — одна tx.
- Защита от config-write-vector: SMTP-host нельзя подменить через config.json без write-доступа к state.db (ACL `%LOCALAPPDATA%`).
- Pydantic-модель Settings БОЛЬШЕ НЕ содержит smtp-секретов и smtp-host/port → diagnostic.zip exclude-list упрощается (state.db.smtp_credentials и так не открывается, см. [[decisions/ADR-012-diagnostic-zip-allowlist-redactor|ADR-012]]).
- Цена: bump `user_version` 1→2 + migration script (R4-M8). Greenfield MVP не имеет prod-баз с v1, но MigrationRunner и ADR должны быть готовы.

См. также: [[decisions-log]], [[decisions/ADR-019-notification-state-machine|ADR-019]], [[data-model/notifications]].
