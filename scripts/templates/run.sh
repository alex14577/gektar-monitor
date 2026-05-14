#!/usr/bin/env bash
# fis-monitor Linux launcher.
# Resolves paths relative to the archive root, sets PLAYWRIGHT_BROWSERS_PATH
# to the bundled Chromium, then exec's the binary — no Python required.
set -euo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers"
exec "$DIR/bin/fis-monitor" --data-dir="$DIR/var" --host=127.0.0.1 --port=8000 "$@"
