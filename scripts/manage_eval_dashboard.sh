#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
LABEL="com.keystone.kba-eval-dashboard"
APP_URL="http://127.0.0.1:8769/dashboard"
HEALTH_URL="http://127.0.0.1:8769/api/status"
PORT="8769"
LOG_DIR="$REPO_ROOT/.keystone/promptfoo/logs"
PID_FILE="$LOG_DIR/eval-dashboard.pid"
STDOUT_LOG="$LOG_DIR/eval-dashboard.stdout.log"
STDERR_LOG="$LOG_DIR/eval-dashboard.stderr.log"
STRUCTURED_LOG="$LOG_DIR/eval-dashboard.structured.jsonl"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
SERVER_SCRIPT="$REPO_ROOT/scripts/serve_promptfoo_eval_dashboard.py"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$LOG_DIR"

is_healthy() {
  curl -fsS "$HEALTH_URL" >/dev/null 2>&1
}

pid_is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

pid_from_file() {
  [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || true
}

port_pids() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

use_launchd() {
  [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1
}

structured_log() {
  local event="${1:-event}"
  local level="${2:-info}"
  local log_status="${3:-}"
  local stage="${4:-dashboard_manager}"
  local message="${5:-}"
  local py="$PYTHON_BIN"
  if [[ ! -x "$py" ]]; then
    py="$(command -v python3 || true)"
  fi
  [[ -n "$py" ]] || return 0
  PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}" "$py" - \
    "$event" "$level" "$log_status" "$stage" "$message" "$APP_URL" "$HEALTH_URL" "$PORT" "$LABEL" \
    "$PID_FILE" "$STDOUT_LOG" "$STDERR_LOG" >> "$STRUCTURED_LOG" 2>/dev/null <<'PY'
import json
import sys

from keystone_agents.structured_logging import structured_log_event

event, level, status, stage, message, app_url, health_url, port, label, pid_file, stdout_log, stderr_log = sys.argv[1:13]
payload = {
    "stage": stage,
    "status": status,
    "message": message,
    "dashboard_url": app_url,
    "health_url": health_url,
    "port": port,
    "launchd_label": label,
    "pid_file": pid_file,
    "stdout_log": stdout_log,
    "stderr_log": stderr_log,
}
print(
    json.dumps(
        structured_log_event(
            component="eval_dashboard_manager",
            event=event,
            level=level,
            payload=payload,
        ),
        ensure_ascii=True,
        sort_keys=True,
    )
)
PY
}

render_launchd_plist() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST_TARGET" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SERVER_SCRIPT</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>$REPO_ROOT</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$STDOUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$STDERR_LOG</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
EOF
}

wait_for_health() {
  for _ in {1..60}; do
    if is_healthy; then
      echo "KNI Evals Dashboard is running at $APP_URL"
      structured_log "health_ok" "info" "ok" "health" "dashboard became healthy"
      return 0
    fi
    sleep 0.25
  done
  echo "KNI Evals Dashboard did not become healthy."
  echo "Logs:"
  echo "  $STDOUT_LOG"
  echo "  $STDERR_LOG"
  echo "  $STRUCTURED_LOG"
  structured_log "health_failed" "error" "error" "health" "dashboard did not become healthy"
  return 1
}

start_service() {
  if is_healthy; then
    echo "KNI Evals Dashboard is already running at $APP_URL"
    return 0
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing Python virtualenv at $PYTHON_BIN"
    structured_log "start_failed" "error" "error" "start" "missing Python virtualenv"
    return 1
  fi
  local existing_pid
  existing_pid="$(pid_from_file)"
  if pid_is_running "$existing_pid"; then
    echo "PID file points to running process $existing_pid, but health check failed."
    echo "Use restart to stop and start cleanly."
    structured_log "start_blocked_stale_health" "error" "error" "start" "PID file process running but health check failed"
    return 1
  fi
  if use_launchd; then
    render_launchd_plist
    launchctl bootout "$GUI_DOMAIN" "$PLIST_TARGET" >/dev/null 2>&1 || true
    launchctl bootstrap "$GUI_DOMAIN" "$PLIST_TARGET"
    launchctl enable "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
    wait_for_health
    return $?
  fi
  cd "$REPO_ROOT" || exit 1
  PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}" nohup "$PYTHON_BIN" "$SERVER_SCRIPT" >> "$STDOUT_LOG" 2>> "$STDERR_LOG" &
  echo "$!" > "$PID_FILE"
  wait_for_health
}

stop_service() {
  local stopped=0
  if use_launchd; then
    if launchctl bootout "$GUI_DOMAIN" "$PLIST_TARGET" >/dev/null 2>&1; then
      stopped=1
    fi
  fi
  local pid
  pid="$(pid_from_file)"
  if pid_is_running "$pid"; then
    kill "$pid"
    stopped=1
  fi
  for pid in ${(f)"$(port_pids)"}; do
    if pid_is_running "$pid"; then
      kill "$pid"
      stopped=1
    fi
  done
  rm -f "$PID_FILE"
  if [[ "$stopped" -eq 1 ]]; then
    echo "Stopped KNI Evals Dashboard."
    structured_log "stop_ok" "info" "ok" "stop" "dashboard process stopped"
  else
    echo "KNI Evals Dashboard was not running."
    structured_log "stop_noop" "info" "ok" "stop" "dashboard was not running"
  fi
}

status_service() {
  local status_code=0
  if is_healthy; then
    echo "KNI Evals Dashboard is running at $APP_URL"
    structured_log "status_ok" "info" "ok" "status" "dashboard reachable"
  else
    echo "KNI Evals Dashboard is not reachable at $APP_URL"
    echo "Remediation: run $0 restart, then check $STDERR_LOG if health still fails."
    structured_log "status_unreachable" "error" "error" "status" "dashboard URL was not reachable"
    status_code=1
  fi
  local pid
  pid="$(pid_from_file)"
  if pid_is_running "$pid"; then
    echo "PID file: $PID_FILE ($pid)"
  elif [[ -n "$pid" ]]; then
    echo "Stale PID file: $PID_FILE ($pid is not running)"
  fi
  local listeners
  listeners="$(port_pids)"
  if [[ -n "$listeners" ]]; then
    echo "Port $PORT listener PID(s): ${(j:, :)${(f)listeners}}"
  elif ! is_healthy; then
    echo "Port $PORT has no listening process."
  fi
  if use_launchd; then
    echo
    echo "launchd:"
    launchctl print "$GUI_DOMAIN/$LABEL" 2>/dev/null || echo "  not loaded"
  fi
  return "$status_code"
}

case "${1:-status}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    sleep 0.5
    start_service
    ;;
  status)
    status_service
    ;;
  open)
    if ! is_healthy; then
      start_service || exit 1
    fi
    open "$APP_URL"
    ;;
  logs)
    tail -80 "$STDOUT_LOG" "$STDERR_LOG" "$STRUCTURED_LOG" 2>/dev/null || true
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|open|logs}"
    exit 2
    ;;
esac
