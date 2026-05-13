# ADR-009: Backup стратегия — только USER_STATE_TABLES

**Context.** Бэкапить весь `state.db` или только user-state?

**Decision.** Только user-state: `lot_user_state`, `notifications`, `smtp_credentials`, `state`. Алгоритм: новая пустая БД → user-state DDL → `executemany` копирование. Размер ~1 МБ, ротация 7 дней, файлы `userstate-YYYY-MM-DD.sqlite` в `data_dir/backups/`.

**Consequences.** Mirror (lots/lots_history/lot_html_archive/cycles/FTS/R-tree) НЕ бэкапим — восстанавливается прогоном. Бэкап маленький, безопасный (нет PII в mirror), быстрый.

См. также: [[decisions-log]], [[architecture/10-8-backup-strategy]].
