# ADR-010: Data_dir location policy

**Context.** Пользователь может разместить data_dir внутри облачного синка (OneDrive/Dropbox/Yandex/`%USERPROFILE%\Documents`). SQLite-WAL + облачный синк = коррапт БД.

**Decision.** В composition root при инициализации — проверка пути. При совпадении с одним из cloud-sync паттернов: `logger.warning` + UI-баннер «БД находится в облачном хранилище — это может привести к повреждению. Перенесите `data_dir`».

**Consequences.** Установщик по умолчанию использует `%LOCALAPPDATA%` (не Documents) — там нет облачного синка by default. Для пользователей, переехавших на нестандартный путь — явное предупреждение.

**Расширение R3-minor (cloud-sync detection — конкретный список паттернов).** В `warn_if_in_cloud_sync(path)` сначала делается `os.path.realpath(path)` (резолв symlinks/junction points). Substring-match по case-insensitive списку: `OneDrive`, `Dropbox`, `Yandex.Disk`, `YandexDisk`, `Google Drive`, `GoogleDrive`, `iCloudDrive`, `pCloud`, `Mega`, `MEGAsync`, `Resilio`, `Sync.com`, `Box`. Для Windows дополнительно: `%USERPROFILE%\Documents`, `%USERPROFILE%\OneDrive`. Линтером не покрывается — только runtime warning + UI-баннер.

См. также: [[decisions-log]], [[architecture/04-composition-root]].
