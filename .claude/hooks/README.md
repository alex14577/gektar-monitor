# Claude Code Hooks

Project-specific hooks for Claude Code and git, enforcing the bd workflow conventions from CLAUDE.md.

## Overview

| Hook | Trigger | Purpose |
|------|---------|---------|
| `stop.sh` | Claude Code session end (Stop event) | Show in-progress bd tasks + available work |
| `post-tool-use-bash.sh` | After every Bash tool call | Vault-update reminder after `git commit`; 4-question vault-check reminder before `bd close` |
| `commit-msg.sh` | git commit-msg hook | Block commits without a beads issue ID `gektar_monitor-XXXX` |

## Claude Code registration

Hooks are registered in `.claude/settings.json` under the `hooks` key. The `Stop` and `PostToolUse` hooks run automatically within Claude Code sessions.

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{ "type": "command", "command": ".claude/hooks/stop.sh" }], "matcher": "" }],
    "PostToolUse": [{ "hooks": [{ "type": "command", "command": ".claude/hooks/post-tool-use-bash.sh" }], "matcher": "Bash" }]
  }
}
```

## Git hook installation

The `commit-msg.sh` hook must be installed into `.git/hooks/` (git hooks are not committed to the repo):

```bash
./scripts/install-hooks.sh          # symlink mode (recommended for dev)
./scripts/install-hooks.sh --copy   # copy mode (CI, shared environments)
```

### Manual install

```bash
ln -sf "$(pwd)/.claude/hooks/commit-msg.sh" .git/hooks/commit-msg
chmod +x .git/hooks/commit-msg
```

## Hook details

### stop.sh — bd-status-on-stop

Runs when Claude Code exits. Prints:
- IN_PROGRESS tasks (to catch work left unclosed)
- Available work from `bd ready`
- Reminder about mandatory push + bd close + bd dolt push

**Performance:** ~700ms — two `bd` CLI invocations (local SQLite, no network).

### post-tool-use-bash.sh — vault-update-check

Receives the tool payload via stdin (JSON with `tool_name`, `tool_input`, `tool_response`).

**After `git commit`:** greps changed files in the latest commit for:
- New/modified `docs/decisions/ADR-*.md` → remind to update `decisions-log.md`
- Modified `docs/glossary.md` → remind to check wiki-links
- New/modified `src/.../domain/` Python files → remind to check glossary and data-model docs

**After `bd close`:** prints the 4-question vault-check (from `feedback_vault_update_after_approve`):
1. New architectural decision not covered by ADR?
2. New term / class / Protocol / pattern?
3. Contract / flow / invariant changed?
4. Known limitation or non-obvious trade-off?

**Performance:** ~20ms — uses `jq` for JSON parsing (falls back to two `python3` spawns ~150ms if `jq` absent), single `git diff` call.

### commit-msg.sh — commit-without-bd-block

Validates that the commit message contains a beads issue ID matching:

```
gektar_monitor-[a-z0-9]+
```

The pattern is intentionally bare (no surrounding parens required) to handle all historical formats:

```
feat(domain): add LotStatus enum (gektar_monitor-0kx)
refactor(web): cleanup (gektar_monitor-6f6, gektar_monitor-4fn)
```

#### Meta-commits (no issue ID)

Some commits are infrastructure/meta work not tracked in bd (e.g. initial setup, merge commits,
wave-plan updates). For these, use `BD_SKIP=1`:

```bash
BD_SKIP=1 git commit -m "docs(waves): update wave-plan (gektar_monitor)"
```

This prints a warning to stderr but allows the commit. The intent is to keep the bypass visible
(auditable in CI logs) while avoiding `--no-verify` which silences all hooks.

**Override options:**

| Method | Behaviour |
|--------|-----------|
| `git commit --no-verify` | Skip all hooks (git built-in) |
| `BD_SKIP=1 git commit ...` | Skip only the bd-id check; prints a warning to stderr |

**Performance:** pure bash regex, no subprocesses beyond `grep`, <10ms.

## Design constraints

- `commit-msg.sh`: <10ms — pure bash + grep, no spawned processes.
- `post-tool-use-bash.sh`: ~20ms with jq, ~150ms fallback with python3 — no network, minimal I/O.
- `stop.sh`: ~700ms — two local `bd` CLI calls (SQLite, no network). Acceptable for session-end.
- Hooks are fail-open (warnings only) except `commit-msg` which hard-fails.
- `jq` is the preferred JSON parser for speed; `python3` fallback for portability.
