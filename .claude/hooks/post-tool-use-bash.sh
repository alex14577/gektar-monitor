#!/usr/bin/env bash
# Claude Code PostToolUse hook — vault-update-check after git commit / bd close
#
# Triggered after every Bash tool call. We inspect the command that was just run.
# If it was a git commit → check if diff contains ADR or glossary changes.
# If it was bd close   → remind about vault-check (4 questions).
#
# Input (stdin): JSON with keys: tool_name, tool_input, tool_response
# Fast: no network, grep on recent git diff only.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Read hook payload from stdin
PAYLOAD="$(cat)"

# Extract the command that was executed.
# Prefer jq (~10ms) over python3 (~75ms each spawn). Fall back to python3 if jq absent.
if command -v jq >/dev/null 2>&1; then
    TOOL_NAME="$(echo "$PAYLOAD" | jq -r '.tool_name // ""' 2>/dev/null || true)"
    COMMAND="$(echo "$PAYLOAD" | jq -r '(.tool_input // {}) | if type == "object" then .command // "" else "" end' 2>/dev/null || true)"
else
    TOOL_NAME="$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || true)"
    COMMAND="$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); inp=d.get('tool_input',{}); print(inp.get('command','') if isinstance(inp,dict) else '')" 2>/dev/null || true)"
fi

if [[ "$TOOL_NAME" != "Bash" ]]; then
    exit 0
fi

# ── bd close branch ────────────────────────────────────────────────────────────
if echo "$COMMAND" | grep -qE '^bd close'; then
    echo ""
    echo "=== VAULT-CHECK (mandatory before bd close) ==="
    echo "Answer all 4 questions explicitly (or write 'vault: no-op'):"
    echo ""
    echo "  1. New architectural decision not covered by existing ADR?"
    echo "     → docs/decisions/ADR-NNN-<slug>.md + link in decisions-log.md"
    echo "  2. New term / class / Protocol / pattern introduced?"
    echo "     → docs/glossary.md"
    echo "  3. Contract / flow / invariant changed in docs/architecture/ or docs/data-model/?"
    echo "     → update relevant file"
    echo "  4. Known limitation or non-obvious trade-off surfaced?"
    echo "     → glossary or §Consequences of relevant ADR"
    echo ""
    echo "If all 4 = no → write to wave-plan.md: 'vault: no-op — <reason>'."
    echo "================================================"
    exit 0
fi

# ── git commit branch ──────────────────────────────────────────────────────────
if echo "$COMMAND" | grep -qE '^git commit'; then
    # Get list of files changed in the latest commit
    CHANGED="$(git diff --name-only HEAD~1 HEAD 2>/dev/null || git diff --cached --name-only 2>/dev/null || true)"

    if [[ -z "$CHANGED" ]]; then
        exit 0
    fi

    WARNINGS=()

    # Check for new ADR files
    NEW_ADRS="$(echo "$CHANGED" | grep -E '^docs/decisions/ADR-' | head -5 || true)"
    if [[ -n "$NEW_ADRS" ]]; then
        WARNINGS+=("ADR file(s) changed: $NEW_ADRS")
        WARNINGS+=("  → Ensure decisions-log.md link is updated.")
    fi

    # Check if glossary changed
    if echo "$CHANGED" | grep -qE '^docs/glossary\.md$'; then
        WARNINGS+=("docs/glossary.md changed — verify all new terms have [[wiki-links]].")
    fi

    # Check for new domain classes (new Python files under domain/)
    NEW_DOMAIN="$(echo "$CHANGED" | grep -E '^src/.*/domain/' | head -5 || true)"
    if [[ -n "$NEW_DOMAIN" ]]; then
        WARNINGS+=("Domain files changed: $NEW_DOMAIN")
        WARNINGS+=("  → New classes/Protocols? Check docs/glossary.md and data-model/.")
    fi

    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        echo ""
        echo "=== VAULT-UPDATE-CHECK (post-commit) ==="
        for W in "${WARNINGS[@]}"; do
            echo "  WARNING: $W"
        done
        echo ""
        echo "Run vault-check (4 questions from feedback_vault_update_after_approve)."
        echo "========================================="
    fi
fi

exit 0
