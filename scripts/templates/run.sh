#!/usr/bin/env bash
# fis-monitor Linux launcher.
# Resolves paths relative to the archive root, sets PLAYWRIGHT_BROWSERS_PATH
# to the bundled Chromium, then exec's the binary — no Python required.
# If the service is already up, just opens the UI in a browser and exits;
# otherwise starts the binary and opens the browser once it responds.
set -euo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers"
# Bundled libnss3/libnspr4/libasound for Chromium on minimal Ubuntu installs
# (bd zclo). Appended so system libs win when present and current.
if [[ -d "$DIR/browsers/_runtime_lib" ]]; then
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$DIR/browsers/_runtime_lib"
fi

URL="http://127.0.0.1:8000/"

open_browser() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "$URL" >/dev/null 2>&1 || true
    fi
}

service_up() {
    curl -fsS -o /dev/null --max-time 1 "$URL" 2>/dev/null
}

if service_up; then
    open_browser
    exit 0
fi

# Background poller: wait up to ~30s for the service to come up, then open the browser.
(
    for _ in $(seq 1 60); do
        if service_up; then
            open_browser
            exit 0
        fi
        sleep 0.5
    done
) &

exec "$DIR/bin/fis-monitor" --data-dir="$DIR/var" --host=127.0.0.1 --port=8000 "$@"
