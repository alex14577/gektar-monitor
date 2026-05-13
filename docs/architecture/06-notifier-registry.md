# 6. Plugin discovery для Notifiers — explicit registry

## Альтернативы

| Подход | Плюсы | Минусы |
|---|---|---|
| **Explicit registry** (composition root вызывает `registry.register(...)`) | прозрачно, типизировано, не зависит от файловой структуры, легко отключить канал в коде | при добавлении канала надо тронуть `composition.py` (1 строка) |
| **Entry points** (`pyproject.toml [project.entry-points]`) | плагины из сторонних пакетов | для onefile-бинаря (Nuitka) entry_points не работают штатно (или работают через костыли); магия |
| **Auto-discover по папке** (`pkgutil.iter_modules(notifiers_pkg)`) | «добавил файл — работает» | плохо контролируется порядок инициализации, скрытые зависимости, ломается в Nuitka |

## Решение

**Explicit registry** для MVP. Обоснование:

1. **Nuitka onefile** — entry_points и auto-discover требуют `__file__`-обхода, который в onefile неконсистентен. Explicit регистрация — гарантированно работает.
2. **Все плагины в MVP — наши**, не из внешних пакетов. «Плагин-маркетплейс» не нужен.
3. **Тестируемость** — в тестах подменяется `registry.register(FakeNotifier)`. Auto-discover требует моков на pkgutil.
4. **Однострочное добавление** — не overhead.

Если в v3+ появится сторонние плагины (например, клиент пишет свой webhook на Python) — переходим на entry_points с fallback на explicit. Сейчас — overengineering.

См. [[decisions/ADR-002-plugin-discovery-explicit-registry|ADR-002]].
