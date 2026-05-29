# Build gektar-gen-license Windows x86_64 standalone executable.
#
# Output: dist\gektar-gen-license-windows-x86_64-<version>.exe
#
# STATUS: Written but NOT tested on a real Windows machine.
# Run this script on a Windows 10/11 x86_64 host with Python 3.12+ installed.
# Verify output before distributing to partners.
#
# Requirements:
#   - Python 3.12+ in PATH
#   - Internet access (pip packages only — NO Playwright/Chromium download)
#   - ~300 MB free disk space
#   - PowerShell 5.1+ (Windows 10+ built-in) or PowerShell 7+
#
# Principles:
#   - Failure-fast: Set-StrictMode + $ErrorActionPreference = "Stop"
#   - DRY: version read once from pyproject.toml
#   - Separate venv (build\.venv-gen): never shares with fis-monitor build\.venv
#
# TODO: version helpers are duplicated from build_release.ps1.
#       Extract to scripts\lib\BuildHelpers.ps1 in a future cleanup task.

# CR-1: param() MUST be the first statement (only comments may precede it).
param(
    [switch]$Clean
)

Set-StrictMode -Version 2
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step { param([string]$msg) Write-Host "[build-gen] $msg" -ForegroundColor Blue }
function Write-Fail  {
    param([string]$msg)
    Write-Error "[build-gen ERROR] $msg"
    exit 1
}

function Get-ProjectVersion {
    # CR-4: Use Python (tomllib) for authoritative parsing — matches sh script approach.
    # read_bytes() avoids Windows cp1252 default, which fails on the UTF-8
    # Russian description in pyproject.toml.
    $ver = & python -c "import tomllib, pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_bytes().decode('utf-8'))['project']['version'])" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Could not parse version from pyproject.toml: $ver"
    }
    return $ver.Trim()
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$ScriptDir   = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir
$BuildDir    = Join-Path $ProjectRoot "build"
$DistDir     = Join-Path $ProjectRoot "dist"
$VenvDir     = Join-Path $BuildDir ".venv-gen"
$PyiDist     = Join-Path $BuildDir "_dist-gen"
$PyiWork     = Join-Path $BuildDir "_work-gen"
$SpecFile    = Join-Path $BuildDir "gektar-gen-license.spec"
$BinaryName  = "gektar-gen-license"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
Set-Location $ProjectRoot

if (-not (Test-Path "pyproject.toml")) {
    Write-Fail "Must be run from project root or via scripts\build_gen_license.ps1"
}
if (-not (Test-Path $SpecFile)) {
    Write-Fail "Spec file not found: $SpecFile"
}

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $pythonExe) {
    Write-Fail "python not found in PATH"
}
$pyVersion = & python --version 2>&1
Write-Step "Detected: $pyVersion"
# CR-2: Enforce Python 3.12+ (matches sh script gate).
& python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Python 3.12+ required (got $pyVersion)"
}

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
$VERSION      = Get-ProjectVersion
$ArtifactName = "$BinaryName-windows-x86_64-$VERSION.exe"
$ArtifactPath = Join-Path $DistDir $ArtifactName

Write-Step "Building version $VERSION → $ArtifactPath"

# ---------------------------------------------------------------------------
# Step 1: Clean stale artefacts
# ---------------------------------------------------------------------------
Write-Step "Cleaning stale build artefacts..."
@($PyiDist) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
}
if ($Clean) {
    Write-Step "  --Clean: wiping venv and PyInstaller work dir"
    @($VenvDir, $PyiWork) | ForEach-Object {
        if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
    }
}

# ---------------------------------------------------------------------------
# Step 2: Create / reuse venv + install project + PyInstaller
# ---------------------------------------------------------------------------
# Separate venv from fis-monitor's build\.venv — prevents playwright and other
# heavy runtime deps from bleeding into the gen-license binary (ADR-059).
if (Test-Path (Join-Path $VenvDir "Scripts\python.exe")) {
    Write-Step "Reusing existing build venv at $VenvDir (use -Clean to recreate)"
    # SE-3: Warn if pyproject.toml changed since venv was last populated.
    $PyprojectHash = (Get-FileHash (Join-Path $ProjectRoot "pyproject.toml") -Algorithm SHA256).Hash
    $VenvHashFile  = Join-Path $VenvDir ".pyproject.sha256"
    if ((Test-Path $VenvHashFile) -and ((Get-Content $VenvHashFile -Raw).Trim() -ne $PyprojectHash)) {
        Write-Step "  WARNING: pyproject.toml has changed since venv was created. Run -Clean to rebuild venv."
    }
} else {
    Write-Step "Creating build venv at $VenvDir..."
    & python -m venv $VenvDir
}

$Pip         = Join-Path $VenvDir "Scripts\pip.exe"
$Python      = Join-Path $VenvDir "Scripts\python.exe"
$PyInstaller = Join-Path $VenvDir "Scripts\pyinstaller.exe"

Write-Step "Installing project and build tools..."
& $Pip install --quiet --upgrade pip
& $Pip install --quiet -e $ProjectRoot
# SE-2: Pin PyInstaller to tested 6.x range.
& $Pip install --quiet "pyinstaller>=6.10,<7"

# SE-3: Record pyproject.toml hash for stale-venv detection.
$PyprojectHash = (Get-FileHash (Join-Path $ProjectRoot "pyproject.toml") -Algorithm SHA256).Hash
$PyprojectHash | Set-Content (Join-Path $VenvDir ".pyproject.sha256") -Encoding UTF8

# Note: playwright is a project dependency (pyproject.toml) and will be
# installed in this venv via pip install -e. That is expected — PyInstaller
# only packages what is reachable from the entry-point import graph.
# The spec's excludes list + import-linter contract gen-license-cli-no-app-graph
# prevent playwright from entering the frozen binary. The size gate below is the
# final safety net.

# ---------------------------------------------------------------------------
# Step 3: Run PyInstaller
# ---------------------------------------------------------------------------
Write-Step "Running PyInstaller (--onefile mode)..."
$PiArgs = @($SpecFile, "--distpath", $PyiDist, "--workpath", $PyiWork, "--noconfirm")
if ($Clean) {
    $PiArgs += "--clean"
}
# SE-4: Wipe partial PyInstaller output on failure so stale artefacts never ship.
try {
    & $PyInstaller @PiArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited with code $LASTEXITCODE"
    }
} catch {
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "PyInstaller failed: $_"
}

$BinaryFile = Join-Path $PyiDist "$BinaryName.exe"
if (-not (Test-Path $BinaryFile)) {
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "PyInstaller output not found at $BinaryFile"
}

# ---------------------------------------------------------------------------
# Step 4: Smoke test
# ---------------------------------------------------------------------------
Write-Step "Running smoke test..."
$SmokeTmp = Join-Path $env:TEMP "gektar-gen-license-smoke-$(Get-Random)"
New-Item -ItemType Directory -Path $SmokeTmp -Force | Out-Null

$NbfDate = (Get-Date -AsUTC).ToString("yyyy-MM-dd")
$ExpDate = (Get-Date -AsUTC).AddDays(1).ToString("yyyy-MM-dd")

& $BinaryFile issue --nbf $NbfDate --exp $ExpDate --out $SmokeTmp
if ($LASTEXITCODE -ne 0) {
    Remove-Item $SmokeTmp -Recurse -Force
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "Smoke test: binary exited with code $LASTEXITCODE"
}

$KeyFile = Join-Path $SmokeTmp "license.key"
if (-not (Test-Path $KeyFile)) {
    Remove-Item $SmokeTmp -Recurse -Force
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "Smoke test: license.key not created in $SmokeTmp"
}

$KeyContent = Get-Content $KeyFile -Raw
if (-not ($KeyContent -match "^v2\.")) {
    Remove-Item $SmokeTmp -Recurse -Force
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "Smoke test: license.key does not start with 'v2.' — got: $($KeyContent.Substring(0, [Math]::Min(40, $KeyContent.Length)))"
}

Remove-Item $SmokeTmp -Recurse -Force
Write-Step "  Smoke test PASSED"

# ---------------------------------------------------------------------------
# Step 5: Size gate (<25 MB)
# ---------------------------------------------------------------------------
$BinarySize = (Get-Item $BinaryFile).Length
$SizeLimit  = 26214400  # 25 MB in bytes
if ($BinarySize -ge $SizeLimit) {
    $SizeMB = [Math]::Round($BinarySize / 1MB, 1)
    if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }
    Write-Fail "Size gate FAILED: binary is ${SizeMB} MB (limit: 25 MB). Check for unexpected deps."
}
$SizeMBDisplay = [Math]::Round($BinarySize / 1MB, 1)
Write-Step "  Size gate PASSED: ${SizeMBDisplay} MB"

# ---------------------------------------------------------------------------
# Step 6: Copy artifact to dist\
# ---------------------------------------------------------------------------
Write-Step "Copying artifact..."
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
Copy-Item $BinaryFile -Destination $ArtifactPath -Force


# ---------------------------------------------------------------------------
# Step 8: Cleanup build intermediates
# ---------------------------------------------------------------------------
Write-Step "Cleaning build intermediates..."
# Keep venv and work dir for incremental rebuilds. Only wipe _dist-gen.
if (Test-Path $PyiDist) { Remove-Item $PyiDist -Recurse -Force }

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Step "SUCCESS"
Write-Step "Artifact : $ArtifactPath"
Write-Step ("Size     : {0:F1} MB" -f $SizeMBDisplay)
