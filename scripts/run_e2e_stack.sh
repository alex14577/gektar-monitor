#!/usr/bin/env bash
# scripts/run_e2e_stack.sh — start fake_torgi + fis-monitor for local e2e dev.
#
# Behaviour: launches both services in the background, waits for their
# health endpoints, prints the addresses, and stays foreground (wait).
# Ctrl+C / SIGTERM triggers graceful shutdown of both processes.
#
# Idempotent: a previous run's PIDs are killed before relaunch. If the
# PID file was deleted but the process is still alive (orphan after a
# crash), pkill -f fallback finishes the cleanup so the port-check below
# doesn't fail with a misleading "port in use" message — bd f5a9.
# Must be run from the project root (where pyproject.toml lives).

set -euo pipefail

# ── Help ─────────────────────────────────────────────────────────────
# bd zqzy: CLI flag support
show_help() {
  cat <<'EOF'
Usage: run_e2e_stack.sh [OPTIONS]

Start fake_torgi + fis-monitor for local e2e development.
Always launches fake_torgi with auth bypass enabled (FAKE_TORGI_NO_AUTH=1)
for local-dev convenience.
Must be run from project root (where pyproject.toml lives).

Options:
  --no-onboarding   Mark onboarding completed in state.db, wait for cache to expire
  --reset           Wipe DATA_DIR (var/) before launch for a clean state.db / fresh feed
  -h, --help        Show this help and exit

Environment variables:
  E2E_FAKE_PORT=N      fake_torgi port (default: 8001)
  E2E_FIS_PORT=N       fis-monitor port (default: 8000)
  E2E_STACK_DIR=PATH   working dir for logs/pids/data (default: /tmp/e2e-stack)

Examples:
  run_e2e_stack.sh
  run_e2e_stack.sh --no-onboarding
  run_e2e_stack.sh --reset --no-onboarding
  E2E_FIS_PORT=9000 run_e2e_stack.sh --no-onboarding
EOF
}

# ── Arg parsing ──────────────────────────────────────────────────────
FLAG_NO_ONBOARDING=0
FLAG_RESET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-onboarding) FLAG_NO_ONBOARDING=1 ;;
    --reset)         FLAG_RESET=1 ;;
    --help|-h)       show_help; exit 0 ;;
    *) printf "Unknown flag: %s\n\n" "$1" >&2; show_help >&2; exit 2 ;;
  esac
  shift
done

# ── Configuration ────────────────────────────────────────────────────
FAKE_PORT="${E2E_FAKE_PORT:-8001}"
FIS_PORT="${E2E_FIS_PORT:-8000}"
STACK_DIR="${E2E_STACK_DIR:-/tmp/e2e-stack}"
DATA_DIR="${STACK_DIR}/var"
FAKE_LOG="${STACK_DIR}/fake_torgi.log"
FIS_LOG="${STACK_DIR}/fis_monitor.log"
FAKE_PID_FILE="${STACK_DIR}/fake_torgi.pid"
FIS_PID_FILE="${STACK_DIR}/fis_monitor.pid"

# ── Helpers ──────────────────────────────────────────────────────────
log() { printf "[e2e] %s\n" "$*"; }
die() { printf "ERROR: %s\n" "$*" >&2; exit 1; }

kill_if_running() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] || return 0
  local pid
  pid="$(<"$pidfile")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "Stopping process PID=$pid"
    kill "$pid" 2>/dev/null || true
    # Give it 2s to exit gracefully, then SIGKILL if still alive
    for _ in 1 2 3 4; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

wait_ready() {
  local url="$1" name="$2" attempts="${3:-30}"
  for i in $(seq 1 "$attempts"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name ready after ${i} probe(s)"
      return 0
    fi
    sleep 0.5
  done
  die "$name did not respond at $url after ${attempts} attempts (see logs in $STACK_DIR)"
}

cleanup() {
  log "Shutting down stack..."
  kill_if_running "$FIS_PID_FILE"
  kill_if_running "$FAKE_PID_FILE"
  log "Stack stopped."
}
trap cleanup EXIT INT TERM

reset_safety_check() {
  # Normalise FIRST so guards see the resolved path (catches `..` traversal and
  # symlinks in $STACK_DIR — without this, /tmp/../etc/x/var would pass /tmp/* check).
  # realpath -m works even when the path doesn't exist yet.
  local real
  real="$(realpath -m -- "$DATA_DIR")" \
                                    || die "--reset: cannot normalise DATA_DIR='$DATA_DIR'."
  # Defense-in-depth: STACK_DIR/DATA_DIR currently always non-empty/absolute via :- default,
  # kept as a tripwire if Configuration block is ever refactored.
  [[ -n "$STACK_DIR" ]]             || die "--reset: E2E_STACK_DIR is empty; refusing to wipe."
  [[ "$real" == /* ]]               || die "--reset: resolved path='$real' is not absolute; refusing to wipe."
  [[ "$real" != "/" && "$real" != "/var" && "$real" != "/tmp" && "$real" != "$HOME/var" ]] \
                                    || die "--reset: resolved path='$real' is a dangerous system path; refusing to wipe."
  local depth
  depth=$(awk -F'/' '{print NF-1}' <<<"$real")
  [[ "$depth" -ge 3 ]]              || die "--reset: resolved path='$real' is too shallow (depth=$depth); refusing to wipe."
  [[ "$real" == /tmp/* || "$real" == "$HOME"/* ]] \
                                    || die "--reset: resolved path='$real' is outside allowed prefixes (/tmp/ or \$HOME/); refusing to wipe."
  # Reject if STACK_DIR itself is exactly $HOME (would resolve DATA_DIR to $HOME/var, masked by allowlist).
  [[ "$STACK_DIR" != "$HOME" ]]     || die "--reset: E2E_STACK_DIR is \$HOME itself; refusing to wipe."
}

# ── Pre-flight checks ────────────────────────────────────────────────
[[ -f pyproject.toml ]] || die "Run from project root (no pyproject.toml here)."

if [[ "$FLAG_NO_ONBOARDING" == "1" ]]; then
  command -v python3 >/dev/null 2>&1 || die "--no-onboarding requires python3 in PATH"
fi

# Run --reset safety check BEFORE mkdir below — otherwise dangerous paths fail
# with confusing "mkdir: Permission denied" instead of a clear "refusing to wipe" message.
if [[ "$FLAG_RESET" == "1" ]]; then
  reset_safety_check
fi

for port in "$FAKE_PORT" "$FIS_PORT"; do
  if ss -ltn 2>/dev/null | grep -q ":${port} "; then
    die "Port $port already in use. Stop the occupying process or set E2E_FAKE_PORT/E2E_FIS_PORT."
  fi
done

# ── Idempotency: clean up previous stack ─────────────────────────────
mkdir -p "$STACK_DIR" "$DATA_DIR"
kill_if_running "$FIS_PID_FILE"
kill_if_running "$FAKE_PID_FILE"
# Orphan-process fallback (bd f5a9): if a previous run crashed and the
# PID file was removed by hand, kill_if_running is a no-op. Match by
# command-line so the port-check below sees a clean slate. Both pkill
# calls tolerate "no matches" (rc=1) via `|| true`.
pkill -f fis-monitor 2>/dev/null || true
pkill -f fake_torgi 2>/dev/null || true
if [[ "$FLAG_RESET" == "1" ]]; then
  # safety_check already ran in pre-flight; just wipe + recreate
  log "--reset: wiping DATA_DIR=$DATA_DIR"
  rm -rf "$DATA_DIR"
  mkdir -p "$DATA_DIR"
fi
rm -f "$DATA_DIR/app.lock"

# ── Auth bypass (always on for local dev) ────────────────────────────
# fake_torgi reads FAKE_TORGI_NO_AUTH and skips ESIA login when truthy.
# Always enabled here — this script is for local development only.
# For headed-login testing run fake_torgi standalone with FAKE_TORGI_NO_AUTH=0.
if [[ -n "${E2E_NO_AUTH+x}" ]]; then
  log "WARNING: E2E_NO_AUTH is set but no longer honoured — auth bypass is always on. Unset it from your shell."
fi
export FAKE_TORGI_NO_AUTH=1
log "Auth bypass ENABLED — /cabinet/* will respond without fake-ESIA login"

# ── Launch fake_torgi ────────────────────────────────────────────────
log "Starting fake_torgi on :${FAKE_PORT}..."
uv run python tools/fake_torgi/server.py \
  --port "$FAKE_PORT" --host 127.0.0.1 \
  >"$FAKE_LOG" 2>&1 &
echo $! >"$FAKE_PID_FILE"
wait_ready "http://127.0.0.1:${FAKE_PORT}/status" "fake_torgi"

# ── Launch fis-monitor ───────────────────────────────────────────────
log "Starting fis-monitor on :${FIS_PORT}..."
FIS_TARGET__BASE_URL="http://127.0.0.1:${FAKE_PORT}" \
LOG_JSON=0 \
FIS_LOG_LEVEL_DEFAULT=DEBUG \
  uv run fis-monitor \
    --host 127.0.0.1 \
    --port "$FIS_PORT" \
    --data-dir "$DATA_DIR" \
  >"$FIS_LOG" 2>&1 &
echo $! >"$FIS_PID_FILE"
wait_ready "http://127.0.0.1:${FIS_PORT}/auth/status" "fis-monitor"

# ── Onboarding bypass ────────────────────────────────────────────────
apply_onboarding_bypass() {
  local db="${DATA_DIR}/state.db"
  log "Marking onboarding_state=completed in ${db}"
  DB_PATH="${db}" python3 - <<'PYEOF'
import sqlite3, datetime, os
db = os.environ["DB_PATH"]
con = sqlite3.connect(db)
# Schema may not be initialised yet if fis-monitor lazy-creates tables — defensive CREATE
con.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
con.execute(
    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
    ("onboarding_state", "completed", datetime.datetime.now(datetime.timezone.utc).isoformat()),
)
con.commit()
con.close()
PYEOF
  # Middleware cache TTL is 1.0s (src/fis_monitor/web/onboarding_gate.py:_CACHE_TTL).
  # Sleep slightly longer so the next request re-reads from DB.
  log "Waiting 1.2s for onboarding middleware cache (TTL=1.0s) to expire..."
  sleep 1.2
  local verify
  verify=$(DB_PATH="${db}" python3 - <<'PYEOF'
import sqlite3, os
con = sqlite3.connect(os.environ["DB_PATH"])
row = con.execute("SELECT value FROM state WHERE key='onboarding_state'").fetchone()
print(row[0] if row else "MISSING")
con.close()
PYEOF
)
  if [[ "$verify" == "completed" ]]; then
    log "Onboarding bypass verified (state=completed)."
  else
    die "Onboarding bypass FAILED — state=${verify} (expected 'completed')"
  fi
}

if [[ "$FLAG_NO_ONBOARDING" == "1" ]]; then
  apply_onboarding_bypass
fi

# ── Summary ──────────────────────────────────────────────────────────
echo
echo "================================================================"
echo "  E2E stack is running"
echo "----------------------------------------------------------------"
printf "  fake_torgi:        http://127.0.0.1:%s\n"        "$FAKE_PORT"
printf "  fake_torgi admin:  http://127.0.0.1:%s/admin\n"  "$FAKE_PORT"
printf "  fake_torgi status: http://127.0.0.1:%s/status\n" "$FAKE_PORT"
printf "  fis-monitor web:   http://127.0.0.1:%s\n"        "$FIS_PORT"
echo
printf "  Logs:    %s/*.log\n" "$STACK_DIR"
printf "  Data:    %s\n"       "$DATA_DIR"
echo  "  Stop:    Ctrl+C"
echo "================================================================"
echo

# ── Foreground wait ──────────────────────────────────────────────────
# Wait for both background PIDs. EXIT trap fires on Ctrl+C or termination.
wait
