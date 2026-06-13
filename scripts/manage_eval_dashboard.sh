#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
APP_URL="http://127.0.0.1:8769/dashboard"
PORT="8769"
LOG_DIR="$REPO_ROOT/.keystone/promptfoo/logs"
PID_FILE="$LOG_DIR/eval-dashboard.pid"
STDOUT_LOG="$LOG_DIR/eval-dashboard.stdout.log"
STDERR_LOG="$LOG_DIR/eval-dashboard.stderr.log"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
SERVER_SCRIPT="$REPO_ROOT/scripts/serve_promptfoo_eval_dashboard.py"

mkdir -p "$LOG_DIR"

is_healthy() {
  curl -fsS "$APP_URL" >/dev/null 2>&1
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

wait_for_health() {
  for _ in {1..60}; do
    if is_healthy; then
      echo "KNI Evals Dashboard is running at $APP_URL"
      return 0
    fi
    sleep 0.25
  done
  echo "KNI Evals Dashboard did not become healthy."
  echo "Logs:"
  echo "  $STDOUT_LOG"
  echo "  $STDERR_LOG"
  return 1
}

start_service() {
  if is_healthy; then
    echo "KNI Evals Dashboard is already running at $APP_URL"
    return 0
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing Python virtualenv at $PYTHON_BIN"
    return 1
  fi
  local existing_pid
  existing_pid="$(pid_from_file)"
  if pid_is_running "$existing_pid"; then
    echo "PID file points to running process $existing_pid, but health check failed."
    echo "Use restart to stop and start cleanly."
    return 1
  fi
  cd "$REPO_ROOT" || exit 1
  PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}" nohup "$PYTHON_BIN" "$SERVER_SCRIPT" >> "$STDOUT_LOG" 2>> "$STDERR_LOG" &
  echo "$!" > "$PID_FILE"
  wait_for_health
}

stop_service() {
  local stopped=0
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
  else
    echo "KNI Evals Dashboard was not running."
  fi
}

status_service() {
  local status_code=0
  if is_healthy; then
    echo "KNI Evals Dashboard is running at $APP_URL"
  else
    echo "KNI Evals Dashboard is not reachable at $APP_URL"
    status_code=1
  fi
  local pid
  pid="$(pid_from_file)"
  if pid_is_running "$pid"; then
    echo "PID file: $PID_FILE ($pid)"
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
    tail -80 "$STDOUT_LOG" "$STDERR_LOG" 2>/dev/null || true
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|open|logs}"
    exit 2
    ;;
esac
