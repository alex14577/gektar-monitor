@echo off
rem fis-monitor Windows launcher.
rem Resolves paths relative to the archive root, sets PLAYWRIGHT_BROWSERS_PATH
rem to the bundled Chromium, then starts the binary — no Python required.
setlocal
set PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers
"%~dp0bin\fis-monitor.exe" --data-dir="%~dp0var" --host=127.0.0.1 --port=8000 %*
