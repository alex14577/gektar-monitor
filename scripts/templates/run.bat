@echo off
rem fis-monitor Windows launcher.
rem Resolves paths relative to the archive root, sets PLAYWRIGHT_BROWSERS_PATH
rem to the bundled Chromium, then starts the binary — no Python required.
rem If the service is already up, just opens the UI in a browser and exits;
rem otherwise starts the binary and opens the browser once it responds.
setlocal
set PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers
set "URL=http://127.0.0.1:8000/"

rem If service is already up, just open the browser and exit.
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop | Out-Null; exit 0 } catch { exit 1 }" >NUL 2>&1
if not errorlevel 1 (
    start "" "%URL%"
    exit /b 0
)

rem Service not running — schedule a background poller that opens the browser
rem once the service responds, then start the binary in the foreground.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Milliseconds 500 } }"

"%~dp0bin\fis-monitor.exe" --data-dir="%~dp0var" --host=127.0.0.1 --port=8000 %*
