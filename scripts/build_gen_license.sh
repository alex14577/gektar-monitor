#!/usr/bin/env bash
# Build gektar-gen-license Linux x86_64 standalone executable.
#
# Output: dist/gektar-gen-license-linux-x86_64-<version>
#         dist/gektar-gen-license-linux-x86_64-<version>.sha256
#
# Principles:
#   - Failure-fast: set -euo pipefail aborts on any error.
#   - DRY: version read once from pyproject.toml.
#   - Reproducible: separate build venv (build/.venv-gen) — never shares with
#     the fis-monitor venv (build/.venv) to prevent dependency bleed.
#   - Extensible: Windows equivalent in build_gen_license.ps1.
#
# Requirements:
#   - Python 3.12+ available as `python3`
#   - Internet access (pip packages only — NO Playwright/Chromium download)
#   - ~300 MB free disk space (venv + PyInstaller artefacts)
#
# Flags:
#   --clean   Wipe venv and PyInstaller work dir before building.
#             Use for release builds to guarantee reproducibility.
#             Default: incremental (venv + work dir reused).
#
# TODO: version() / log() / die() helpers are duplicated from build_release.sh.
#       Extract to scripts/lib/build_helpers.sh in a future cleanup task.

set -euo pipefail

CLEAN_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN_BUILD=1 ;;
        *) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '\033[1;34m[build-gen]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build-gen ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

version() {
    # Read version from pyproject.toml using only stdlib — no extra deps.
    python3 - <<'PYEOF'
import tomllib, pathlib
# read_bytes() avoids platform default-encoding issues (cp1252 on Windows
# chokes on the UTF-8 Russian description); harmless on Linux.
data = tomllib.loads(pathlib.Path("pyproject.toml").read_bytes().decode("utf-8"))
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
VENV_DIR="$BUILD_DIR/.venv-gen"
PYINSTALLER_DIST="$BUILD_DIR/_dist-gen"
PYINSTALLER_WORK="$BUILD_DIR/_work-gen"
SPEC_FILE="$BUILD_DIR/gektar-gen-license.spec"
BINARY_NAME="gektar-gen-license"

# ---------------------------------------------------------------------------
# Validate preconditions
# ---------------------------------------------------------------------------
cd "$PROJECT_ROOT"

[[ -f pyproject.toml ]] || die "Must be run from project root or via scripts/build_gen_license.sh"
[[ -f "$SPEC_FILE" ]] || die "Spec file not found: $SPEC_FILE"

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log "Python $PYTHON_VERSION detected"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" \
    || die "Python 3.12+ required (got $PYTHON_VERSION)"

# ---------------------------------------------------------------------------
# Read version (single source of truth: pyproject.toml)
# ---------------------------------------------------------------------------
VERSION=$(version)
ARTIFACT_NAME="${BINARY_NAME}-linux-x86_64-${VERSION}"
ARTIFACT_PATH="$DIST_DIR/${ARTIFACT_NAME}"

log "Building version $VERSION → $ARTIFACT_PATH"

# ---------------------------------------------------------------------------
# Step 1: Clean stale build artefacts
# ---------------------------------------------------------------------------
# Always wipe _dist-gen (must be fresh on every run).
# $VENV_DIR and $PYINSTALLER_WORK are cached for incremental builds; pass
# --clean to force their removal for a reproducible release build.
log "Cleaning stale build artefacts..."
rm -rf "$PYINSTALLER_DIST"
if [[ $CLEAN_BUILD -eq 1 ]]; then
    log "  --clean: wiping venv and PyInstaller work dir"
    rm -rf "$VENV_DIR" "$PYINSTALLER_WORK"
fi

# ---------------------------------------------------------------------------
# Step 2: Create / reuse venv + install project + PyInstaller
# ---------------------------------------------------------------------------
# Separate venv from fis-monitor's build/.venv — prevents playwright and other
# heavy runtime deps from bleeding into the gen-license binary (ADR-059).
# If `uv` is on PATH, use it for 10-50× faster installs.
if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
    log "Reusing existing build venv at $VENV_DIR (use --clean to recreate)"
    # SE-3: Warn if pyproject.toml changed since venv was last populated.
    PYPROJECT_HASH=$(sha256sum "$PROJECT_ROOT/pyproject.toml" | cut -d' ' -f1)
    VENV_HASH_FILE="$VENV_DIR/.pyproject.sha256"
    if [[ -f "$VENV_HASH_FILE" && "$(cat "$VENV_HASH_FILE")" != "$PYPROJECT_HASH" ]]; then
        log "  WARNING: pyproject.toml has changed since venv was created. Run --clean to rebuild venv."
    fi
else
    log "Creating build venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

if command -v uv >/dev/null 2>&1; then
    INSTALLER=(uv pip install --python "$PYTHON" --quiet)
    log "Installing project and build tools (uv)..."
else
    INSTALLER=("$PIP" install --quiet)
    log "Installing project and build tools (pip — install \`uv\` for 10× speedup)..."
fi
"${INSTALLER[@]}" --upgrade pip
"${INSTALLER[@]}" -e "$PROJECT_ROOT"
"${INSTALLER[@]}" "pyinstaller>=6.10,<7"

# Record pyproject.toml hash for stale-venv detection (SE-3).
sha256sum "$PROJECT_ROOT/pyproject.toml" | cut -d' ' -f1 > "$VENV_DIR/.pyproject.sha256"

# Note: playwright is a project dependency (pyproject.toml) and will be
# installed in this venv via `pip install -e .`. That is expected — PyInstaller
# only packages what is reachable from the entry-point import graph.
# The spec's `excludes` list + import-linter contract `gen-license-cli-no-app-graph`
# prevent playwright from entering the frozen binary. The size gate below is the
# final safety net: if playwright hooks somehow snuck in, the binary would
# exceed 25 MB and the build would fail before the artefact ships.

# ---------------------------------------------------------------------------
# Step 3: Run PyInstaller
# ---------------------------------------------------------------------------
log "Running PyInstaller (--onefile mode)..."
PI_FLAGS=(--distpath "$PYINSTALLER_DIST" --workpath "$PYINSTALLER_WORK" --noconfirm)
if [[ $CLEAN_BUILD -eq 1 ]]; then
    PI_FLAGS+=(--clean)
fi
# SE-4: Wipe partial PyInstaller output on failure so stale artefacts never ship.
trap 'rm -rf "$PYINSTALLER_DIST"' ERR
"$VENV_DIR/bin/pyinstaller" "$SPEC_FILE" "${PI_FLAGS[@]}"
trap - ERR

BINARY_FILE="$PYINSTALLER_DIST/$BINARY_NAME"
[[ -f "$BINARY_FILE" ]] || { rm -rf "$PYINSTALLER_DIST"; die "PyInstaller output not found at $BINARY_FILE"; }

# ---------------------------------------------------------------------------
# Step 4: Smoke test
# ---------------------------------------------------------------------------
log "Running smoke test..."
SMOKE_TMPDIR=$(mktemp -d)
trap 'rm -rf "$SMOKE_TMPDIR"' EXIT

NBF_DATE=$(date -u +%Y-%m-%d)
EXP_DATE=$(date -u -d '+1 day' +%Y-%m-%d)

"$BINARY_FILE" issue --nbf "$NBF_DATE" --exp "$EXP_DATE" --out "$SMOKE_TMPDIR" \
    || { rm -rf "$PYINSTALLER_DIST"; die "Smoke test: binary exited non-zero"; }

[[ -f "$SMOKE_TMPDIR/license.key" ]] \
    || { rm -rf "$PYINSTALLER_DIST"; die "Smoke test: license.key not created in $SMOKE_TMPDIR"; }

grep -q '^v2\.' "$SMOKE_TMPDIR/license.key" \
    || { rm -rf "$PYINSTALLER_DIST"; die "Smoke test: license.key does not start with 'v2.' — got: $(head -1 "$SMOKE_TMPDIR/license.key")"; }

log "  Smoke test PASSED — license.key: $(head -1 "$SMOKE_TMPDIR/license.key" | cut -c1-40)..."

# ---------------------------------------------------------------------------
# Step 5: Size gate (<25 MB)
# ---------------------------------------------------------------------------
BINARY_SIZE=$(stat -c %s "$BINARY_FILE")
SIZE_LIMIT=26214400  # 25 MB in bytes
if [[ $BINARY_SIZE -ge $SIZE_LIMIT ]]; then
    SIZE_MB=$(( BINARY_SIZE / 1048576 ))
    rm -rf "$PYINSTALLER_DIST"
    die "Size gate FAILED: binary is ${SIZE_MB} MB (limit: 25 MB). Check for unexpected deps."
fi
SIZE_HR=$(du -sh "$BINARY_FILE" | cut -f1)
log "  Size gate PASSED: $SIZE_HR"

# ---------------------------------------------------------------------------
# Step 6: Copy artifact to dist/
# ---------------------------------------------------------------------------
log "Copying artifact..."
mkdir -p "$DIST_DIR"
cp "$BINARY_FILE" "$ARTIFACT_PATH"
chmod 755 "$ARTIFACT_PATH"

# ---------------------------------------------------------------------------
# Step 7: SHA256 checksum
# ---------------------------------------------------------------------------
sha256sum "$ARTIFACT_PATH" > "${ARTIFACT_PATH}.sha256"
log "SHA-256: $(cat "${ARTIFACT_PATH}.sha256")"

# ---------------------------------------------------------------------------
# Step 8: Cleanup build intermediates
# ---------------------------------------------------------------------------
log "Cleaning build intermediates..."
# Keep venv ($VENV_DIR) and work dir ($PYINSTALLER_WORK) for incremental
# rebuilds. Only wipe _dist-gen (already copied to dist/).
rm -rf "$PYINSTALLER_DIST"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "SUCCESS"
log "Artifact : $ARTIFACT_PATH"
log "Checksum : ${ARTIFACT_PATH}.sha256"
log "Size     : $SIZE_HR"
