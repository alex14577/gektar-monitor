---
id: ADR-026
title: Distribution packaging — PyInstaller --onedir + bundled Chromium
status: accepted
date: 2026-05-14
---

## Context

The application must be distributed to end-clients as a self-contained archive. The client:
- Has a GUI operating system (Ubuntu Desktop or Windows 10/11).
- Has no Python, pyenv, pip, or Playwright installed.
- Should unpack the archive, run `run.sh` / `run.bat`, and open `http://127.0.0.1:8000` — nothing else.

The application embeds Playwright Chromium for Gosuslugi OAuth (headed browser required).

## Decision

### PyInstaller --onedir (not --onefile)

`--onedir` mode places the binary and all bundled libraries in a directory tree (`bin/_internal/`). The binary runs directly from that directory.

`--onefile` wraps everything in a single executable that extracts to `$TMPDIR` on every launch — adding 5-15 seconds of cold-start latency and requiring writable `/tmp`. Rejected.

### Bundled Chromium (not post-install)

The `browsers/` directory is included in the archive alongside `bin/`. The launcher sets `PLAYWRIGHT_BROWSERS_PATH=$DIR/browsers` before exec'ing the binary. The client never runs `playwright install`.

Alternative — ship a post-install `setup.sh` that downloads Chromium — was rejected: it requires internet on the client machine at install time and creates a two-step UX that is easy to break.

### Build-on-target cross-platform strategy

Linux archive is built on a Linux machine; Windows archive is built on a Windows machine. No cross-compilation.

Nuitka was considered: it produces smaller binaries but has significantly more complex build configuration and limited Playwright support. Rejected in favour of PyInstaller's first-class Playwright hook (`hook-playwright.sync_api.py`).

Docker was considered for distribution: rejected because it requires Docker daemon on client and adds significant operational overhead for a GUI application.

source+install.sh was considered: rejected because it requires Python 3.12, pip, and internet access on the client.

### Resource path resolution

`src/fis_monitor/web/templates.py` uses `Path(__file__).parent / "templates"`. In `--onedir` mode, `__file__` for all package modules resolves to `_internal/fis_monitor/…`, so `Path(__file__).parent / "templates"` resolves to `_internal/fis_monitor/web/templates/` — exactly where `--add-data` places the templates. No `sys._MEIPASS` patching needed.

`src/fis_monitor/composition.py` uses `Path(__file__).resolve().parent.parent.parent / "docs/db/schema.sql"`. In `--onedir` mode this resolves to `_internal/docs/db/schema.sql`. The spec bundles `docs/db/schema.sql` with destination `docs/db/` so it lands at the correct path.

### Archive format

- Linux: `.tar.gz` (preserves POSIX executable bits on `bin/fis-monitor` and `run.sh`).
- Windows: `.zip` (native Windows tooling).

## Consequences

- **Archive size**: ~340 MB (Chromium ≈ 280 MB, Python runtime + deps ≈ 60 MB).
- **Update mechanism**: new archive per release — client unpacks new version alongside old. `var/` data directory is portable between versions within the same major version.
- **Chromium security updates**: tied to Playwright's release cycle and therefore to fis-monitor's release cycle. Clients receive Chromium updates only when a new fis-monitor archive is distributed.
- **macOS / ARM**: adding a new target requires a new build script (`scripts/build_release_macos.sh` or `scripts/build_release_linux_arm64.sh`) and a corresponding build machine. No changes to this spec or existing scripts.
- **Build prerequisites**: Python 3.12+, internet access (~2 GB Playwright Chromium download), ~2 GB free disk.

## See also

- [[operations/release-build]] — how to run the build, troubleshooting
- [[decisions/ADR-010-data-dir-location-policy|ADR-010]] — `--data-dir` CLI flag and default location
