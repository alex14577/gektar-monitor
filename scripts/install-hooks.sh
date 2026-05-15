#!/usr/bin/env bash
# Install project git hooks into .git/hooks/
#
# Usage:
#   ./scripts/install-hooks.sh           # symlink mode (default)
#   ./scripts/install-hooks.sh --copy    # copy mode (for CI or shared envs)
#
# After install, git commits that lack a beads issue ID (gektar_monitor-XXXX)
# will be blocked. Use BD_SKIP=1 or --no-verify to bypass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_SRC="$REPO_ROOT/.claude/hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

MODE="symlink"
if [[ "${1:-}" == "--copy" ]]; then
    MODE="copy"
fi

install_hook() {
    local src="$1"
    local dst_name="$2"
    local dst="$HOOKS_DST/$dst_name"

    if [[ -e "$dst" && ! -L "$dst" ]]; then
        echo "  SKIP: $dst_name already exists (not a symlink). Remove manually to replace."
        return
    fi

    if [[ "$MODE" == "symlink" ]]; then
        ln -sf "$src" "$dst"
        echo "  LINKED: $dst_name → $src"
    else
        cp "$src" "$dst"
        chmod +x "$dst"
        echo "  COPIED: $dst_name ← $src"
    fi
    chmod +x "$dst"
}

echo "Installing gektar_monitor git hooks ($MODE mode)..."
echo ""

install_hook "$HOOKS_SRC/commit-msg.sh" "commit-msg"

echo ""
echo "Done. Hooks installed in $HOOKS_DST/"
echo ""
echo "Installed hooks:"
echo "  commit-msg  — blocks commits without (gektar_monitor-XXXX) in message"
echo ""
echo "Override: BD_SKIP=1 git commit ...  or  git commit --no-verify"
