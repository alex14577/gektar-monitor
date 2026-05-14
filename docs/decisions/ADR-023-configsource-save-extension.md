# ADR-023: `ConfigSource.save(Settings)` extends ConfigSource Protocol (no SettingsWriter split)

**Context.** `bd:2s3` требует мутации частей `Settings` через HTTP (POST `/settings/regions`, POST `/settings/recipients`). Read-side `ConfigSource.current()/subscribe()` уже есть. Нужна write-seam. Опции:

1. **Extend ConfigSource** — добавить `save(Settings)` к Protocol; `WatchdogConfigSource` реализует атомарную замену JSON-файла. Watchdog ловит свой же `FileMovedEvent.dest_path` → ре-loads → `subscribe()` колбэки выстреливают на той же шине, что и внешние правки файла.
2. **Separate SettingsWriter Protocol** — отдельный seam для записи; routes зависят от `SettingsWriter`, read-callers — от `ConfigSource`.

**Decision.** Вариант 1 — расширить `ConfigSource` методом `save(settings: Settings) -> None`. Реализация в `WatchdogConfigSource`: `os.replace` на temp-файле (atomic POSIX rename) → watchdog ловит `FileMovedEvent.dest_path` (BA-5 из session #6 brainstorm) → существующий reload-path публикует изменения подписчикам. SHA-256 content-hash dedup (уже есть для внешних правок) корректно отрабатывает self-write (новый hash → reload; identical hash → silent no-op).

**Rationale.**
- Locality: read и write касаются одного JSON-файла; разносить по двум Protocol'ам — искусственное расщепление SRP (Protocol == «работа с конфиг-файлом»).
- Один subscribe-bus для обоих источников правок (UI POST + ручной edit файла) — нет специальных кодпотоков «после save() надо вручную дёргать колбэки».
- ISP не нарушен: все текущие callers `ConfigSource` уже зависят от полной поверхности `current()+subscribe()`; добавление `save()` не вынуждает их менять контракт (новый метод не вызывают).
- `SettingsWriter` имел бы смысл, если бы был множественный backend записи (DB + file) — это не наш случай.

**Альтернатива отвергнута.** Отдельный `SettingsWriter` потребовал бы координации между двумя Protocol'ами (как обеспечить, что write-side инвалидирует read-side cache), вводил бы риск двух SSOT, и не давал бы выигрыша в тестируемости (DI оба варианта поддерживают одинаково).

**Consequences.**
- `ConfigSource` Protocol теперь включает `save(settings: Settings) -> None`.
- Реализация атомарна: temp-файл в той же папке + `os.replace`. Race с inotify reload недопустима — `WatchdogConfigSource` использует `_lock` (уже есть для reload).
- Self-reload через watchdog — единственный путь публикации `Settings` в subscribers; нет «in-process shortcut» (избегаем двух кодпотоков с разной семантикой).
- Hot-reload latency после `save()` ограничена временем inotify→handler (millisec range).
- POST routes (`/settings/regions`, `/settings/recipients`) делают `current() → mutate → save()` — это compute-and-replace pattern. Race между конкурентными POST'ами решается advisory: web-сервер однопоточный (uvicorn single worker) + повторный reload корректно мерджит.
- Тестируемость: in-memory `FakeConfigSource` тривиально реализует `save()` как `self._current = settings`.

См. также: [[decisions-log]], [[decisions/ADR-013-config-source-watchdog|ADR-013]] (источник watchdog-pattern), [[architecture/03-protocols]].
