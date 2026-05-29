# Release build — gektar-gen-license standalone executable

How to produce a partner-deliverable executable from source.

See [[decisions/ADR-059-gen-license-standalone-onefile|ADR-059]] for rationale on --onefile mode.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | `python3 --version`. pyenv works. |
| Internet access | pip packages only (~50 MB — NO Playwright/Chromium download) |
| ~300 MB free disk | Build venv + PyInstaller artefacts |
| VCRedist 14.x (Windows only) | Usually pre-installed on Windows 10/11. If the .exe crashes immediately on a reseller's machine, install [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) (x64). |

No GUI required. No Chromium. No `playwright install`.

## Building Linux x86_64

```bash
# From project root:
bash scripts/build_gen_license.sh
```

Output: `dist/gektar-gen-license-linux-x86_64-<version>` + `.sha256` checksum.

The script:
1. Creates isolated venv under `build/.venv-gen` (separate from `build/.venv`)
2. Installs the project + PyInstaller (no playwright)
3. Runs PyInstaller (`build/gektar-gen-license.spec`, `--onefile`)
4. Runs smoke test: `issue --nbf <today> --exp <today+1d> --out <tmpdir>`
5. Checks size gate: binary must be <25 MB
6. Copies `dist/gektar-gen-license-linux-x86_64-<version>`
7. Writes SHA256 checksum

For a clean (fully reproducible) release build:

```bash
bash scripts/build_gen_license.sh --clean
```

**Expected duration**: clean build ~3-5 min (venv creation + pip + PyInstaller + smoke test); incremental build ~1-2 min (venv reused, PyInstaller work dir cached).

## Building Windows x86_64

```powershell
# From project root in PowerShell 5.1+ or PowerShell 7+:
.\scripts\build_gen_license.ps1
```

Output: `dist\gektar-gen-license-windows-x86_64-<version>.exe` + `.sha256`.

For a clean build:

```powershell
.\scripts\build_gen_license.ps1 -Clean
```

**Expected duration**: clean build ~3-5 min; incremental ~1-2 min.

**Status: written, NOT tested on a real Windows machine.** Run on a Windows 10/11
x86_64 host with Python 3.12 in PATH and verify the artefact before distributing.

## Smoke test (manual)

```bash
TMPDIR=$(mktemp -d)
./dist/gektar-gen-license-linux-x86_64-1.0.0 \
    issue \
    --nbf $(date -u +%Y-%m-%d) \
    --exp $(date -u -d '+30 days' +%Y-%m-%d) \
    --out "$TMPDIR"

cat "$TMPDIR/license.key"   # must start with v2.
```

Expected output: a single line `v2.<base64-payload>.<base64-sig>`.

## Artifact layout

```
dist/
├── gektar-gen-license-linux-x86_64-<ver>        # single executable (Linux)
├── gektar-gen-license-linux-x86_64-<ver>.sha256
├── gektar-gen-license-windows-x86_64-<ver>.exe  # single executable (Windows)
└── gektar-gen-license-windows-x86_64-<ver>.exe.sha256
```

Unlike fis-monitor, there is no directory structure or `browsers/` — a single
file is the complete deliverable.

## Distributing to partners

Send the appropriate binary + README-gen-license.txt to the authorized partner.

```bash
# Example: pack for email
zip gektar-gen-license-linux-x86_64-1.0.0.zip \
    dist/gektar-gen-license-linux-x86_64-1.0.0 \
    dist/gektar-gen-license-linux-x86_64-1.0.0.sha256 \
    scripts/templates/README-gen-license.txt
```

## Troubleshooting

### Size gate failure (>25 MB)

**Symptom**: `Size gate FAILED: binary is XX MB (limit: 25 MB)`

**Cause**: a dependency accidentally pulled in a heavy package (playwright,
requests, fastapi, etc.).

**Fix**:
1. Run `build/.venv-gen/bin/pip list` — check for unexpected packages.
2. Check `.importlinter` contract `gen-license-cli-no-app-graph` — run
   `lint-imports` to see if a forbidden import was introduced.
3. Run PyInstaller with `--log-level DEBUG` to see the full import graph.

### Missing module at runtime

**Symptom**: `ModuleNotFoundError: No module named 'foo'` on first run.

**Fix**: add `'foo'` to `hiddenimports` in `build/gektar-gen-license.spec`.
This should be rare — the CLI uses only stdlib + `fis_monitor.licensing.*`
which are all statically imported.

### Smoke test: `date -d` not available

On macOS or BSD, `date -d` is not supported. Use:
```bash
# macOS
EXP=$(python3 -c "from datetime import date, timedelta; print((date.today()+timedelta(1)).isoformat())")
```

### Windows SmartScreen warning

Expected behaviour for an unsigned binary. Partner must click:
"More info" → "Run anyway"

Documented in `scripts/templates/README-gen-license.txt`.

### venv contaminated with playwright

If the guard step fails (`playwright found in build/.venv-gen`):
```bash
rm -rf build/.venv-gen
bash scripts/build_gen_license.sh
```

Then investigate which transitive dependency introduced playwright.

## See also

- [[decisions/ADR-059-gen-license-standalone-onefile|ADR-059]] — rationale for --onefile, size gate, isolated venv
- [[decisions/ADR-057-licensing-cli-as-entry-point|ADR-057]] — CLI as console_script, PyInstaller invariant
- [[decisions/ADR-026-distribution-packaging-pyinstaller|ADR-026]] — fis-monitor distribution (--onedir, Chromium)
- [[operations/release-build|release-build]] — fis-monitor release build runbook
