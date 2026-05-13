# 10.8 Backup-стратегия — user-state only

После ревью DBA: **бэкапим только user-state**, не весь `state.db`. Mirror восстановим с сайта.

**`USER_STATE_TABLES`** — явный список:
- `lot_user_state` (starred / submitted / note)
- `notifications` (idempotency-журнал)
- `smtp_credentials` (логин/пароль)
- `state` (KV — onboarding, last_visit, dnd_until)

**Алгоритм** (`BackupService.backup_user_state(dst_path)`):
1. Открыть НОВУЮ пустую БД по `dst_path`.
2. Применить DDL только для `USER_STATE_TABLES`.
3. Из текущей `state.db` сделать `SELECT *` каждой таблицы и пагинировать через `cur.fetchmany(1000)` — для каждой пачки `executemany INSERT` в новую БД. Это держит память ограниченной даже при росте `notifications` за год.
4. Закрыть, atomic rename.

**Размер**: ~1 МБ. **Ротация**: 7 дней. **Имя**: `userstate-YYYY-MM-DD.sqlite` в `data_dir/backups/`.

**Mirror НЕ бэкапим** — `lots`, `lots_history`, `lot_html_archive`, `cycles`, FTS, R-tree. Они восстанавливаются полным переразбором (есть HTML-архив) либо новым прогоном.

> **Альтернатива** (рассмотренная и отвергнутая): `VACUUM INTO 'backup.db'` всего `state.db`. Проще, но (а) бэкап раздувается до десятков МБ через год; (б) при восстановлении из бэкапа на новой машине mirror «застывает» — лучше пусть новый клиент разберёт сайт заново.

См. [[decisions/ADR-009-backup-user-state-tables-only|ADR-009]].
