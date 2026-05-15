#!/usr/bin/env bash
# git commit-msg hook — enforce bd issue ID in commit message
#
# Blocks commits whose message does NOT contain a beads issue ID of the form
#   (gektar_monitor-XXXX)
# where XXXX is one or more alphanumeric characters.
#
# Override options:
#   BD_SKIP=1 git commit ...        — skip check (prints warning, allows commit)
#   git commit --no-verify ...      — bypass all hooks (git built-in)
#
# Install: copy/symlink to .git/hooks/commit-msg and chmod +x
# Or run: scripts/install-hooks.sh

set -euo pipefail

COMMIT_MSG_FILE="${1}"
COMMIT_MSG="$(cat "$COMMIT_MSG_FILE")"

# Pattern: gektar_monitor-abc123  — bare ID anywhere in the message (no paren required).
# Handles all historical formats:
#   (gektar_monitor-0kx)                 — single ID
#   (gektar_monitor-6f6, gektar_monitor-4fn)  — multi-ID
# Meta-commits without an ID must use BD_SKIP=1 (see README).
BD_ID_PATTERN='gektar_monitor-[a-z0-9]+'

if echo "$COMMIT_MSG" | grep -qE "$BD_ID_PATTERN"; then
    exit 0
fi

# Allow override via env-var (with warning)
if [[ "${BD_SKIP:-0}" == "1" ]]; then
    echo "WARNING: BD_SKIP=1 — commit-msg bd-id check bypassed." >&2
    echo "  Commit message: $(echo "$COMMIT_MSG" | head -1)" >&2
    exit 0
fi

echo "" >&2
echo "ERROR: commit blocked — no beads issue ID found in commit message." >&2
echo "" >&2
echo "  Expected pattern: gektar_monitor-XXXX somewhere in the message." >&2
echo "  Examples:" >&2
echo "    feat(domain): add LotStatus enum (gektar_monitor-0kx)" >&2
echo "    refactor(web): cleanup (gektar_monitor-6f6, gektar_monitor-4fn)" >&2
echo "  Meta-commits without an ID: use BD_SKIP=1 (see .claude/hooks/README.md)." >&2
echo "" >&2
echo "  To bypass:" >&2
echo "    git commit --no-verify   # skip all hooks" >&2
echo "    BD_SKIP=1 git commit ...  # skip bd-id check only (shows warning)" >&2
echo "" >&2
exit 1
