# ADR-002: Plugin discovery — explicit registry, не entry_points

**Context.** Notifier-каналы — плагины. Варианты discovery: entry_points, auto-discover, explicit registry.

**Decision.** Explicit registry в composition root. **Nuitka onefile** ломает entry_points (требуется `__file__`-обход, в onefile неконсистентен). Supply-chain — entry_points позволяет сторонним пакетам инжектировать notifier без явного согласия. В MVP все каналы — наши.

**Consequences.** Добавление канала = новый класс + 1 строка в `composition.py`. Никакой магии при импорте. При появлении сторонних плагинов (v3+) — миграция на entry_points с fallback.

См. также: [[decisions-log]], [[architecture/06-notifier-registry]], [[notifications]].
