# Build fis-monitor Windows x86_64 distribution archive.
#
# Output: dist\fis-monitor-windows-x86_64-<version>.zip
#         dist\fis-monitor-windows-x86_64-<version>.zip.sha256
#
# STATUS: Written but NOT tested on a real Windows machine.
# Run this script on a Windows 10/11 x86_64 host with Python 3.12+ installed.
# Verify output before distributing to clients.
#
# Requirements:
#   - Python 3.12+ in PATH
#   - Internet access (pip + Playwright Chromium download)
#   - ~2 GB free disk space
#   - PowerShell 5.1+ (Windows 10+ built-in) or PowerShell 7+
#
# Principles:
#   - Failure-fast: Set-StrictMode + $ErrorActionPreference = "Stop"
#   - DRY: version read once from pyproject.toml
#   - Extensible: macOS/ARM add their own script; this file is Windows-only

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step { param([string]$msg) Write-Host "[build] $msg" -ForegroundColor Blue }
function Write-Fail  { param([string]$msg) Write-Error "[build ERROR] $msg" }

function Get-ProjectVersion {
    $content = Get-Content "$PSScriptRoot\..\pyproject.toml" -Raw
    if ($content -match 'version\s*=\s*"([^"]+)"') {
        return $Matches[1]
    }
    Write-Fail "Could not parse version from pyproject.toml"
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$ScriptDir   = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir
$BuildDir    = Join-Path $ProjectRoot "build"
$DistDir     = Join-Path $ProjectRoot "dist"
$VenvDir     = Join-Path $BuildDir ".venv"
$BrowsersDir = Join-Path $BuildDir "_browsers"
$PyiDist     = Join-Path $BuildDir "_dist"
$PyiWork     = Join-Path $BuildDir "_work"
$StageDir    = Join-Path $BuildDir "_stage"
$TemplatesDir = Join-Path $ScriptDir "templates"
$SpecFile    = Join-Path $BuildDir "fis-monitor.spec"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
Set-Location $ProjectRoot

if (-not (Test-Path "pyproject.toml")) {
    Write-Fail "Must be run from project root or via scripts\build_release.ps1"
}
if (-not (Test-Path $SpecFile)) {
    Write-Fail "Spec file not found: $SpecFile"
}
if (-not (Test-Path $TemplatesDir)) {
    Write-Fail "Templates dir not found: $TemplatesDir"
}

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $pythonExe) {
    Write-Fail "python not found in PATH"
}
$pyVersion = & python --version 2>&1
Write-Step "Detected: $pyVersion"

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
$VERSION      = Get-ProjectVersion
$ArchiveName  = "fis-monitor-windows-x86_64-$VERSION"
$ArchivePath  = Join-Path $DistDir "$ArchiveName.zip"

Write-Step "Building version $VERSION → $ArchivePath"

# ---------------------------------------------------------------------------
# Step 1: Clean
# ---------------------------------------------------------------------------
Write-Step "Cleaning stale build artefacts..."
@($VenvDir, $PyiDist, $PyiWork, $StageDir) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
}
# NB: $BrowsersDir не удаляется — playwright install chromium идемпотентен
# и переиспользует уже скачанный Chromium. Чтобы форсировать перекачку —
# Remove-Item build\_browsers -Recurse -Force вручную.

# ---------------------------------------------------------------------------
# Step 2: venv + install
# ---------------------------------------------------------------------------
Write-Step "Creating build venv..."
& python -m venv $VenvDir

$Pip    = Join-Path $VenvDir "Scripts\pip.exe"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$Playwright = Join-Path $VenvDir "Scripts\playwright.exe"
$PyInstaller = Join-Path $VenvDir "Scripts\pyinstaller.exe"

Write-Step "Installing project and build tools..."
& $Pip install --quiet --upgrade pip
& $Pip install --quiet -e $ProjectRoot
& $Pip install --quiet pyinstaller

# ---------------------------------------------------------------------------
# Step 3: Download Playwright Chromium
# ---------------------------------------------------------------------------
Write-Step "Downloading Playwright Chromium..."
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
& $Playwright install chromium

# ---------------------------------------------------------------------------
# Step 4: PyInstaller
# ---------------------------------------------------------------------------
Write-Step "Running PyInstaller..."
& $PyInstaller $SpecFile `
    --distpath $PyiDist `
    --workpath $PyiWork `
    --clean `
    --noconfirm

$BinaryDir = Join-Path $PyiDist "fis-monitor"
if (-not (Test-Path $BinaryDir)) {
    Write-Fail "PyInstaller output not found at $BinaryDir"
}
$BinaryExe = Join-Path $BinaryDir "fis-monitor.exe"
if (-not (Test-Path $BinaryExe)) {
    Write-Fail "Binary fis-monitor.exe not found in $BinaryDir"
}

# ---------------------------------------------------------------------------
# Step 5: Stage
# ---------------------------------------------------------------------------
Write-Step "Assembling staging tree..."
$StageRoot = Join-Path $StageDir "fis-monitor"
New-Item -ItemType Directory -Force -Path $StageRoot | Out-Null

# bin/ ← PyInstaller --onedir output (rename dir to bin/)
Copy-Item $BinaryDir -Destination (Join-Path $StageRoot "bin") -Recurse

# browsers/ ← downloaded Chromium
Copy-Item $BrowsersDir -Destination (Join-Path $StageRoot "browsers") -Recurse

# Launcher (Windows only — run.sh принадлежит Linux-сборке) and README
Copy-Item (Join-Path $TemplatesDir "run.bat")   (Join-Path $StageRoot "run.bat")
Copy-Item (Join-Path $TemplatesDir "README.txt") (Join-Path $StageRoot "README.txt")

# Verify _internal/ is present
$Internal = Join-Path $StageRoot "bin\_internal"
if (-not (Test-Path $Internal)) {
    Write-Fail "PyInstaller _internal/ not found in staging bin\"
}

# ---------------------------------------------------------------------------
# Step 6: Pack ZIP
# ---------------------------------------------------------------------------
Write-Step "Packing ZIP archive..."
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# Compress-Archive from $StageDir so top-level entry is "fis-monitor/"
Compress-Archive -Path (Join-Path $StageDir "fis-monitor") -DestinationPath $ArchivePath -Force

$ArchiveSize = (Get-Item $ArchivePath).Length / 1MB
Write-Step ("Archive size: {0:F1} MB" -f $ArchiveSize)

# ---------------------------------------------------------------------------
# Step 7: Checksum
# ---------------------------------------------------------------------------
$Hash = (Get-FileHash $ArchivePath -Algorithm SHA256).Hash.ToLower()
"$Hash  $ArchivePath" | Set-Content "$ArchivePath.sha256" -Encoding UTF8
Write-Step "SHA-256: $Hash"

# ---------------------------------------------------------------------------
# Step 8: Cleanup
# ---------------------------------------------------------------------------
Write-Step "Cleaning build intermediates..."
@($VenvDir, $PyiDist, $PyiWork, $StageDir) | ForEach-Object {
    if (Test-Path $_) { Remove-Item $_ -Recurse -Force }
}
# NB: $BrowsersDir не удаляется — playwright install chromium идемпотентен
# и переиспользует уже скачанный Chromium. Чтобы форсировать перекачку —
# Remove-Item build\_browsers -Recurse -Force вручную.

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Step "SUCCESS"
Write-Step "Artifact : $ArchivePath"
Write-Step "Checksum : $ArchivePath.sha256"
Write-Step ("Size     : {0:F1} MB" -f $ArchiveSize)
