#!/usr/bin/env bash
# Build fis-monitor Linux x86_64 distribution archive.
#
# Output: dist/fis-monitor-linux-x86_64-<version>.tar.gz
#         dist/fis-monitor-linux-x86_64-<version>.tar.gz.sha256
#
# Principles:
#   - Failure-fast: set -euo pipefail aborts on any error.
#   - DRY: version read once from pyproject.toml.
#   - Reproducible: fresh venv each run; build/ cleaned after success.
#   - Extensible: new platforms add their own script; this file is Linux-only.
#
# Requirements:
#   - Python 3.12+ available as `python3`
#   - Internet access (pip + Playwright Chromium download)
#   - ~2 GB free disk space under the project root (for venv + Chromium)
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

version() {
    # Read version from pyproject.toml using only stdlib — no extra deps.
    python3 - <<'PYEOF'
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
print(data["project"]["version"])
PYEOF
}

# ---------------------------------------------------------------------------
# Paths (all absolute to avoid cwd sensitivity)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
BUILD_DIR="$PROJECT_ROOT/build"
DIST_DIR="$PROJECT_ROOT/dist"
VENV_DIR="$BUILD_DIR/.venv"
BROWSERS_DIR="$BUILD_DIR/_browsers"
PYINSTALLER_DIST="$BUILD_DIR/_dist"
PYINSTALLER_WORK="$BUILD_DIR/_work"
STAGE_DIR="$BUILD_DIR/_stage"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
SPEC_FILE="$BUILD_DIR/fis-monitor.spec"

# ---------------------------------------------------------------------------
# Validate preconditions
# ---------------------------------------------------------------------------
cd "$PROJECT_ROOT"

[[ -f pyproject.toml ]] || die "Must be run from project root or via scripts/build_release.sh"
[[ -f "$SPEC_FILE" ]] || die "Spec file not found: $SPEC_FILE"
[[ -d "$TEMPLATES_DIR" ]] || die "Templates dir not found: $TEMPLATES_DIR"

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log "Python $PYTHON_VERSION detected"
[[ "$PYTHON_VERSION" == "3."* ]] || die "Python 3.x required"

# ---------------------------------------------------------------------------
# Read version (single source of truth: pyproject.toml)
# ---------------------------------------------------------------------------
VERSION=$(version)
ARCHIVE_NAME="fis-monitor-linux-x86_64-${VERSION}"
ARCHIVE_PATH="$DIST_DIR/${ARCHIVE_NAME}.tar.gz"

log "Building version $VERSION → $ARCHIVE_PATH"

# ---------------------------------------------------------------------------
# Step 1: Clean stale build artefacts
# ---------------------------------------------------------------------------
log "Cleaning stale build artefacts..."
rm -rf "$VENV_DIR" "$BROWSERS_DIR" "$PYINSTALLER_DIST" "$PYINSTALLER_WORK" "$STAGE_DIR"

# ---------------------------------------------------------------------------
# Step 2: Create isolated venv + install project + PyInstaller
# ---------------------------------------------------------------------------
log "Creating build venv..."
python3 -m venv "$VENV_DIR"
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

log "Installing project and build tools..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "$PROJECT_ROOT"
"$PIP" install --quiet pyinstaller

# ---------------------------------------------------------------------------
# Step 3: Download bundled Playwright Chromium
# ---------------------------------------------------------------------------
log "Downloading Playwright Chromium (this may take a while)..."
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" "$VENV_DIR/bin/playwright" install chromium

# ---------------------------------------------------------------------------
# Step 4: Run PyInstaller
# ---------------------------------------------------------------------------
log "Running PyInstaller..."
"$VENV_DIR/bin/pyinstaller" \
    "$SPEC_FILE" \
    --distpath "$PYINSTALLER_DIST" \
    --workpath "$PYINSTALLER_WORK" \
    --clean \
    --noconfirm

BINARY_DIR="$PYINSTALLER_DIST/fis-monitor"
[[ -d "$BINARY_DIR" ]] || die "PyInstaller output not found at $BINARY_DIR"
[[ -f "$BINARY_DIR/fis-monitor" ]] || die "Binary fis-monitor not found in $BINARY_DIR"

# ---------------------------------------------------------------------------
# Step 5: Assemble staging tree
# ---------------------------------------------------------------------------
log "Assembling staging tree..."
STAGE_ROOT="$STAGE_DIR/fis-monitor"
mkdir -p "$STAGE_ROOT"

# bin/ ← PyInstaller --onedir output
cp -r "$BINARY_DIR" "$STAGE_ROOT/bin"

# browsers/ ← downloaded Chromium
cp -r "$BROWSERS_DIR" "$STAGE_ROOT/browsers"

# Launcher scripts and README
install -m 0755 "$TEMPLATES_DIR/run.sh" "$STAGE_ROOT/run.sh"
install -m 0644 "$TEMPLATES_DIR/run.bat" "$STAGE_ROOT/run.bat"
install -m 0644 "$TEMPLATES_DIR/README.txt" "$STAGE_ROOT/README.txt"

# Verify expected layout
[[ -d "$STAGE_ROOT/bin/_internal" ]] || die "PyInstaller _internal/ not found in staging bin/"

# ---------------------------------------------------------------------------
# Step 6: Pack archive
# ---------------------------------------------------------------------------
log "Packing archive..."
mkdir -p "$DIST_DIR"
tar -czf "$ARCHIVE_PATH" -C "$STAGE_DIR" fis-monitor/

ARCHIVE_SIZE=$(du -sh "$ARCHIVE_PATH" | cut -f1)
log "Archive size: $ARCHIVE_SIZE"

# ---------------------------------------------------------------------------
# Step 7: Checksum
# ---------------------------------------------------------------------------
sha256sum "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"
log "SHA-256: $(cat "${ARCHIVE_PATH}.sha256")"

# ---------------------------------------------------------------------------
# Step 8: Cleanup build intermediates
# ---------------------------------------------------------------------------
log "Cleaning build intermediates..."
rm -rf "$VENV_DIR" "$BROWSERS_DIR" "$PYINSTALLER_DIST" "$PYINSTALLER_WORK" "$STAGE_DIR"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "SUCCESS"
log "Artifact : $ARCHIVE_PATH"
log "Checksum : ${ARCHIVE_PATH}.sha256"
log "Size     : $ARCHIVE_SIZE"
