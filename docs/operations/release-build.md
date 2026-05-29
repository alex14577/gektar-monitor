# Release build — fis-monitor distribution archives

How to produce a client-deliverable archive from source.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | `python3 --version`. pyenv works. |
| Internet access | pip packages + Playwright Chromium (~280 MB) |
| ~2 GB free disk | Build venv + Chromium + PyInstaller artefacts |
| GUI OS (for smoke test) | `$DISPLAY` must be set for run.sh smoke test |

## Building Linux x86_64

```bash
# From project root:
bash scripts/build_release.sh
```

Output: `dist/fis-monitor-linux-x86_64-<version>.tar.gz`.

The script:
1. Creates a fresh isolated venv under `build/.venv`
2. Installs the project + PyInstaller
3. Downloads Playwright Chromium to `build/_browsers/`
4. Runs PyInstaller (`build/fis-monitor.spec`, `--onedir`)
5. Assembles staging tree in `build/_stage/`
6. Packs `dist/fis-monitor-linux-x86_64-<version>.tar.gz`
7. Cleans all build intermediates (`build/.venv`, `build/_browsers`, etc.)

## Building Windows x86_64

```powershell
# From project root in PowerShell 5.1+ or PowerShell 7+:
.\scripts\build_release.ps1
```

Output: `dist\fis-monitor-windows-x86_64-<version>.zip`.

**Status: written, NOT tested on a real Windows machine.** Run on a Windows 10/11 x86_64 host with Python 3.12 in PATH and verify the archive before distributing.

## Smoke test (Linux)

```bash
rm -rf /tmp/fis-smoke
mkdir /tmp/fis-smoke
tar -xzf dist/fis-monitor-linux-x86_64-<version>.tar.gz -C /tmp/fis-smoke
cd /tmp/fis-smoke/fis-monitor
./run.sh &
sleep 5
curl -sf http://127.0.0.1:8000/      # must return HTTP 200 with HTML
kill %1
```

## Archive layout

```
fis-monitor/
├── bin/                      # PyInstaller --onedir output
│   ├── fis-monitor           # executable
│   └── _internal/            # Python runtime + bundled libs + data
│       ├── fis_monitor/web/templates/
│       ├── fis_monitor/web/static/
│       └── docs/db/schema.sql
├── browsers/                 # Playwright Chromium (bundled, ~280 MB)
│   └── chromium-<rev>/
├── run.sh                    # Linux launcher
├── run.bat                   # Windows launcher
└── README.txt
```

`var/` and `config.json` are created at runtime in the directory where `run.sh` / `run.bat` is invoked.

## Troubleshooting PyInstaller

### Missing hidden import
**Symptom**: `ModuleNotFoundError: No module named 'foo'` at runtime.

**Fix**: add `'foo'` to the `hiddenimports` list in `build/fis-monitor.spec`.

### Missing data file
**Symptom**: `FileNotFoundError` for a resource file.

**Fix**: add a `(src_path, dest_dir)` tuple to `datas` in the spec. For a file resolved via `Path(__file__).parent / "subdir/file"`, the dest must be `package/subdir/` so the path relative to `_internal/` matches.

### `Path(__file__).resolve().parent.parent.parent` resolves incorrectly
**Symptom**: schema.sql or another file not found.

**Explanation**: in `--onedir` mode, `__file__` for `fis_monitor/composition.py` is `_internal/fis_monitor/composition.py`. Three `.parent` calls reach `_internal/`. Files must be bundled to match this path.

### UPX corruption
UPX is disabled (`upx=False`) in the spec. Do not enable it — UPX can corrupt Playwright's Chromium helper binaries, causing `ExecutablePath is not executable` errors.

### Playwright `BrowserNotFound` at runtime
**Symptom**: `playwright._impl._errors.Error: Executable doesn't exist`.

**Cause**: `PLAYWRIGHT_BROWSERS_PATH` is not set or points to an empty directory.

**Fix**: always launch via `run.sh` / `run.bat` which sets the env var. Do not invoke the binary directly without setting `PLAYWRIGHT_BROWSERS_PATH`.

## Adding a new platform (macOS, ARM)

1. Create `scripts/build_release_<platform>.sh` (or `.ps1`).
2. Add a `run_<platform>.sh` (or launcher variant) to `scripts/templates/`.
3. Add a README section for the new platform.
4. Do NOT modify the existing spec or Linux/Windows build scripts.

The spec (`build/fis-monitor.spec`) is platform-neutral; it works on any host PyInstaller supports.

## CI: GitHub Actions

Workflow: `.github/workflows/release.yml`

### Триггеры

| Событие | Что происходит |
|---|---|
| `git push origin vX.Y.Z` | Собирает обе платформы, создаёт GitHub Release |
| Run workflow (Actions UI) | Собирает обе платформы, артефакты доступны 90 дней; Release **не** создаётся |

### Pipeline

```
push tag v* ──► build (ubuntu-latest)   ──► release (create GitHub Release)
             └► build (windows-latest) ─┘
```

`fail-fast: false` — падение Windows-билда не отменяет Linux-билд.

### Jobs

**`build`** (matrix: ubuntu-latest / windows-latest):
1. `actions/checkout@v4`
2. `actions/setup-python@v5` с Python 3.12, кэш pip
3. `actions/cache@v4` для Playwright Chromium — путь `build/_browsers`, ключ `playwright-<OS>-<hash(pyproject.toml)>`
4. Запуск скрипта сборки с `PLAYWRIGHT_BROWSERS_PATH=$GITHUB_WORKSPACE/build/_browsers`:
   - Linux: `bash scripts/build_release.sh`
   - Windows: `pwsh -ExecutionPolicy Bypass -File scripts/build_release.ps1`
5. `actions/upload-artifact@v4` — загружает `dist/*`, retention 90 дней

**`release`** (только на тег-пуш, depends on build):
1. `actions/download-artifact@v4` — скачивает все артефакты
2. `softprops/action-gh-release@v2` — создаёт Release с автоматическим changelog и прикладывает `.tar.gz` / `.zip`

### Где смотреть логи

Actions tab → выбрать запуск → выбрать job (ubuntu-latest / windows-latest).

Полный лог через CLI:
```bash
gh run list --workflow=release.yml   # найти run ID
gh run view <run-id> --log           # весь лог
gh run view <run-id> --log --job build  # только build-job
```

### Скачивание артефактов

```bash
gh run download <run-id>             # скачать все артефакты в текущий каталог
gh run download <run-id> -n fis-monitor-Linux   # только Linux
gh run download <run-id> -n fis-monitor-Windows # только Windows
```

### Восстановление упавшего Windows-билда

1. Смотреть лог: `gh run view <id> --log` — найти строку `[build ERROR]` или PowerShell exception.
2. Частые причины:
   - `python` не найден в PATH (setup-python добавляет `python`, не `python3`)
   - Path-separator `\` в строках, которые передаются в shell-контекст
   - Line endings CRLF в скрипте (если редактировалось на Windows)
3. Фикс: минимальное изменение в `scripts/build_release.ps1`, commit, push тега заново или ручной dispatch.
4. После 3 итераций без результата — оставить Linux зелёным, задокументировать known issue в README.

### Первый прогон

Playwright Chromium (~280 MB) скачивается заново при первом запуске или при изменении `pyproject.toml`. Это нормально — последующие прогоны используют кэш.

## See also

- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — rationale for PyInstaller --onedir, bundled Chromium, build-on-target
- [[decisions/ADR-010-data-dir-location-policy|ADR-010]] — `--data-dir` location policy
