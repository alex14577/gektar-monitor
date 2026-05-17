#!/usr/bin/env bash
# scripts/run_e2e_stack.sh — start fake_torgi + fis-monitor for local e2e dev.
#
# Behaviour: launches both services in the background, waits for their
# health endpoints, prints the addresses, and stays foreground (wait).
# Ctrl+C / SIGTERM triggers graceful shutdown of both processes.
#
# Idempotent: a previous run's PIDs are killed before relaunch.
# Must be run from the project root (where pyproject.toml lives).

set -euo pipefail

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

# ── Pre-flight checks ────────────────────────────────────────────────
[[ -f pyproject.toml ]] || die "Run from project root (no pyproject.toml here)."

for port in "$FAKE_PORT" "$FIS_PORT"; do
  if ss -ltn 2>/dev/null | grep -q ":${port} "; then
    die "Port $port already in use. Stop the occupying process or set E2E_FAKE_PORT/E2E_FIS_PORT."
  fi
done

# ── Idempotency: clean up previous stack ─────────────────────────────
mkdir -p "$STACK_DIR" "$DATA_DIR"
kill_if_running "$FIS_PID_FILE"
kill_if_running "$FAKE_PID_FILE"
rm -f "$DATA_DIR/app.lock"

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
  uv run fis-monitor \
    --host 127.0.0.1 \
    --port "$FIS_PORT" \
    --data-dir "$DATA_DIR" \
  >"$FIS_LOG" 2>&1 &
echo $! >"$FIS_PID_FILE"
wait_ready "http://127.0.0.1:${FIS_PORT}/auth/status" "fis-monitor"

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
