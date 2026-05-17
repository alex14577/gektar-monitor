#!/usr/bin/env bash
# fis-monitor Linux launcher.
# Resolves paths relative to the archive root, sets PLAYWRIGHT_BROWSERS_PATH
# to the bundled Chromium, then exec's the binary — no Python required.
set -euo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers"
# Bundled libnss3/libnspr4/libasound for Chromium on minimal Ubuntu installs
# (bd zclo). Appended so system libs win when present and current.
if [[ -d "$DIR/browsers/_runtime_lib" ]]; then
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$DIR/browsers/_runtime_lib"
fi
exec "$DIR/bin/fis-monitor" --data-dir="$DIR/var" --host=127.0.0.1 --port=8000 "$@"
