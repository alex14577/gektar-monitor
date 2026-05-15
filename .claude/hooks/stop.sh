#!/usr/bin/env bash
# Claude Code Stop hook — bd-status-on-stop
#
# Runs when Claude Code session ends (Stop event).
# Shows in-progress tasks and available work so nothing is left stranded.
#
# Fast: no network calls, pure local bd reads.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== bd session close check ==="
echo ""

echo "--- IN_PROGRESS tasks ---"
bd list --status=in_progress 2>/dev/null || true

echo ""
echo "--- Available work (bd ready) ---"
bd ready 2>/dev/null || true

echo ""
echo "Reminder: work is NOT done until git push succeeds + bd close + bd dolt push."
