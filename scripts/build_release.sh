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
#
# Flags:
#   --clean   Wipe venv, PyInstaller work dir, and PyInstaller cache before
#             building.  Use this for release builds to guarantee a fully
#             reproducible artefact from scratch.  Default behaviour is
#             incremental (venv reused, PyInstaller analysis cache reused) —
#             archive is still a real, complete distribution.
set -euo pipefail

CLEAN_BUILD=0
DEBUG_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN_BUILD=1 ;;
        --debug) DEBUG_BUILD=1 ;;
        *) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

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
if [[ $DEBUG_BUILD -eq 1 ]]; then
    SPEC_FILE="$BUILD_DIR/fis-monitor-debug.spec"
    BINARY_NAME="fis-monitor-debug"
else
    SPEC_FILE="$BUILD_DIR/fis-monitor.spec"
    BINARY_NAME="fis-monitor"
fi

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
ARCHIVE_NAME="${BINARY_NAME}-linux-x86_64-${VERSION}"
ARCHIVE_PATH="$DIST_DIR/${ARCHIVE_NAME}.tar.gz"

log "Building version $VERSION → $ARCHIVE_PATH"

# ---------------------------------------------------------------------------
# Step 1: Clean stale build artefacts
# ---------------------------------------------------------------------------
# Always wipe _dist and _stage (they MUST be fresh — staging tree is what
# becomes the archive, and PyInstaller's --distpath is rewritten anyway).
# $VENV_DIR and $PYINSTALLER_WORK are cached for incremental builds; pass
# --clean to force their removal for a fully reproducible release build.
# $BROWSERS_DIR is never auto-wiped — playwright install chromium is
# idempotent and reuses the already-downloaded Chromium (~280 MB).
# To force redownload: rm -rf build/_browsers manually.
log "Cleaning stale build artefacts..."
rm -rf "$PYINSTALLER_DIST" "$STAGE_DIR"
if [[ $CLEAN_BUILD -eq 1 ]]; then
    log "  --clean: wiping venv and PyInstaller work dir"
    rm -rf "$VENV_DIR" "$PYINSTALLER_WORK"
fi

# ---------------------------------------------------------------------------
# Step 2: Create / reuse venv + install project + PyInstaller
# ---------------------------------------------------------------------------
# Incremental path: venv is reused across runs. `pip install -e .` is
# idempotent and fast (~1-3 s on warm cache) — only re-resolves if
# pyproject.toml dependencies changed. If `uv` is on PATH, use it for
# pip operations (10-50× faster on cold cache, ~100× on warm).
if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
    log "Reusing existing build venv (use --clean to recreate)"
else
    log "Creating build venv..."
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
"${INSTALLER[@]}" pyinstaller

# ---------------------------------------------------------------------------
# Step 3: Download bundled Playwright Chromium
# ---------------------------------------------------------------------------
# Playwright pins a hardcoded list of supported host platforms per release.
# On a newer Ubuntu than the pinned playwright version knows about, install
# fails with "Playwright does not support chromium on ubuntuXX.YY-x64".
# Ubuntu LTS releases are ABI-compatible enough for the prebuilt Chromium —
# fall back to the newest known LTS via PLAYWRIGHT_HOST_PLATFORM_OVERRIDE
# (supported since playwright 1.42). Manual override via env var is respected.
# Read distro identity in a subshell so /etc/os-release variables don't
# leak into this script's namespace (the file is designed for sourcing per
# freedesktop spec, but sourcing pollutes our locals).
PW_OVERRIDE="${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}"
if [[ -z "$PW_OVERRIDE" && -r /etc/os-release ]]; then
    os_id=$(. /etc/os-release && printf '%s' "${ID:-}")
    os_ver=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
    case "$os_id" in
        ubuntu)
            # Bump both the supported list AND the fallback target when
            # upgrading playwright (e.g. 1.59+ adds ubuntu26.04).
            case "$os_ver" in
                20.04|22.04|24.04) ;;  # natively supported by playwright 1.58
                *)
                    # Override must include arch suffix — registry table is keyed
                    # by "ubuntuXX.YY-x64", not "ubuntuXX.YY". Without suffix the
                    # download URL lookup returns empty and install aborts.
                    arch=$(uname -m)
                    case "$arch" in
                        x86_64) pw_arch="x64" ;;
                        aarch64|arm64) pw_arch="arm64" ;;
                        # ARM32 (armv7l/armhf) is intentionally unsupported —
                        # playwright ships no chromium build for it.
                        *) die "Unsupported arch for playwright override: $arch" ;;
                    esac
                    PW_OVERRIDE="ubuntu24.04-${pw_arch}"
                    log "Unknown Ubuntu ${os_ver:-?} — overriding playwright host to ${PW_OVERRIDE}"
                    ;;
            esac
            ;;
        "") ;;  # /etc/os-release without ID — skip silently
        *) log "Non-ubuntu Linux (${os_id}), skipping playwright host override" ;;
    esac
fi

log "Downloading Playwright Chromium (this may take a while)..."
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$PW_OVERRIDE" \
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" \
    "$VENV_DIR/bin/playwright" install chromium

# ---------------------------------------------------------------------------
# Step 3b: Bundle runtime .so for Chromium (zero-deps tarball — bd zclo)
# ---------------------------------------------------------------------------
# Chromium needs libnss3, libnspr4, libasound that minimal Ubuntu installs
# lack. Fetch the .deb files via apt-get download (no sudo, no install),
# extract them, copy .so into $BROWSERS_DIR/_runtime_lib/. run.sh adds this
# directory to LD_LIBRARY_PATH at launch time.
RUNTIME_LIB="$BROWSERS_DIR/_runtime_lib"
if [[ -d "$RUNTIME_LIB" && -n "$(ls -A "$RUNTIME_LIB" 2>/dev/null)" ]]; then
    log "Runtime lib bundle already present (delete $RUNTIME_LIB to refresh)"
else
    log "Bundling Chromium runtime libs (libnss3, libnspr4, libasound)..."
    DEBS_DIR="$BUILD_DIR/_debs"
    rm -rf "$DEBS_DIR" && mkdir -p "$DEBS_DIR" "$RUNTIME_LIB"
    (
        cd "$DEBS_DIR"
        # apt-get download writes .deb files to cwd. No root needed.
        # Try the modern Ubuntu 24.04+ package name first; on failure, retry
        # with the legacy name. Stderr is preserved so real errors (network,
        # missing apt index) surface — only the "package not found" line is
        # noise we filter out.
        if ! apt-get download libnss3 libnspr4 libasound2t64 2> >(grep -v "Unable to locate package" >&2); then
            log "  libasound2t64 unavailable, retrying with libasound2..."
            apt-get download libnss3 libnspr4 libasound2 \
                || die "apt-get download failed — need apt-based Linux build host"
        fi
        for deb in ./*.deb; do
            dpkg-deb -x "$deb" .
        done
    )
    # Copy .so and preserve versioned symlinks (libasound.so.2 → .so.2.0.0).
    # x86_64-only — the .deb extraction path is ABI-specific; revisit when
    # arm64 builds are needed (parity with arch-detection above).
    cp -a "$DEBS_DIR"/usr/lib/x86_64-linux-gnu/*.so* "$RUNTIME_LIB/"
    log "  bundled $(ls "$RUNTIME_LIB" | wc -l) entries into $RUNTIME_LIB"
    rm -rf "$DEBS_DIR"
fi

# ---------------------------------------------------------------------------
# Step 4: Run PyInstaller
# ---------------------------------------------------------------------------
log "Running PyInstaller..."
# --clean wipes both _work/ AND the PyInstaller global cache (~/.cache/pyinstaller),
# which forces full re-analysis of every dependency. We skip it on incremental
# builds — PyInstaller is idempotent and reuses cached TOC/PYZ when sources are
# unchanged. --noconfirm still overwrites existing _dist without prompting.
PI_FLAGS=(--distpath "$PYINSTALLER_DIST" --workpath "$PYINSTALLER_WORK" --noconfirm)
if [[ $CLEAN_BUILD -eq 1 ]]; then
    PI_FLAGS+=(--clean)
fi
"$VENV_DIR/bin/pyinstaller" "$SPEC_FILE" "${PI_FLAGS[@]}"

BINARY_DIR="$PYINSTALLER_DIST/$BINARY_NAME"
[[ -d "$BINARY_DIR" ]] || die "PyInstaller output not found at $BINARY_DIR"
[[ -f "$BINARY_DIR/$BINARY_NAME" ]] || die "Binary $BINARY_NAME not found in $BINARY_DIR"

# ---------------------------------------------------------------------------
# Step 5: Assemble staging tree
# ---------------------------------------------------------------------------
log "Assembling staging tree..."
STAGE_ROOT="$STAGE_DIR/$BINARY_NAME"
mkdir -p "$STAGE_ROOT"

# bin/ ← PyInstaller --onedir output
cp -r "$BINARY_DIR" "$STAGE_ROOT/bin"

# browsers/ ← downloaded Chromium
cp -r "$BROWSERS_DIR" "$STAGE_ROOT/browsers"

# Launcher script (Linux only — run.bat принадлежит Windows-сборке) and README
install -m 0755 "$TEMPLATES_DIR/run.sh" "$STAGE_ROOT/run.sh"
install -m 0644 "$TEMPLATES_DIR/README.txt" "$STAGE_ROOT/README.txt"

# Verify expected layout
[[ -d "$STAGE_ROOT/bin/_internal" ]] || die "PyInstaller _internal/ not found in staging bin/"

# ---------------------------------------------------------------------------
# Step 6: Pack archive
# ---------------------------------------------------------------------------
log "Packing archive..."
mkdir -p "$DIST_DIR"
# Use pigz (parallel gzip) if available — ~4-6× faster than gzip on multicore
# for a 350-900 MB tarball. Output format is identical (.tar.gz, gzip stream).
if command -v pigz >/dev/null 2>&1; then
    tar -cf - -C "$STAGE_DIR" "$BINARY_NAME/" | pigz > "$ARCHIVE_PATH"
else
    log "  (install \`pigz\` for parallel gzip — 4-6× faster)"
    tar -czf "$ARCHIVE_PATH" -C "$STAGE_DIR" "$BINARY_NAME/"
fi

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
# Only wipe what cannot be cached: _dist (PyInstaller raw output, already
# staged) and _stage (staging tree, already packed). $VENV_DIR,
# $PYINSTALLER_WORK and $BROWSERS_DIR are kept for incremental rebuilds —
# pass --clean to wipe venv + work on the next run for a reproducible
# release artefact.
rm -rf "$PYINSTALLER_DIST" "$STAGE_DIR"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "SUCCESS"
log "Artifact : $ARCHIVE_PATH"
log "Checksum : ${ARCHIVE_PATH}.sha256"
log "Size     : $ARCHIVE_SIZE"
