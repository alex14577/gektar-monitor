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

Output: `dist/fis-monitor-linux-x86_64-<version>.tar.gz` + `.sha256` checksum.

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

Output: `dist\fis-monitor-windows-x86_64-<version>.zip` + `.sha256`.

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

## See also

- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — rationale for PyInstaller --onedir, bundled Chromium, build-on-target
- [[decisions/ADR-010-data-dir-location-policy|ADR-010]] — `--data-dir` location policy
