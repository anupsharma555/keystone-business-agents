#!/bin/zsh
set -euo pipefail

LABEL="com.keystone-business-agents.searxng-headless"
SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${KBA_REPO_ROOT:-${SCRIPT_DIR:h}}"
PLIST_SOURCE="${ROOT_DIR}/launchd/${LABEL}.plist"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
STDOUT_LOG="${ROOT_DIR}/.local/logs/searxng-headless.stdout.log"
STDERR_LOG="${ROOT_DIR}/.local/logs/searxng-headless.stderr.log"
GUI_DOMAIN="gui/$(id -u)"
RUN_SCRIPT="${ROOT_DIR}/scripts/run_searxng_headless.sh"
STOP_SCRIPT="${ROOT_DIR}/scripts/stop_searxng_headless.sh"
PROFILE="${SEARXNG_COLIMA_PROFILE:-kba-searxng}"
PORT="${SEARXNG_PORT:-18080}"
COMPOSE_PROJECT="${SEARXNG_COMPOSE_PROJECT:-kba-searxng}"
DOCKER_CONTEXT="${SEARXNG_DOCKER_CONTEXT:-colima-${PROFILE}}"
if [[ "${PROFILE}" == "default" && -z "${SEARXNG_DOCKER_CONTEXT:-}" ]]; then
  DOCKER_CONTEXT="colima"
fi

usage() {
  cat <<'EOF'
Usage: scripts/manage_searxng_headless.sh <command>

Commands:
  install    Copy the launch agent into ~/Library/LaunchAgents and load it
  uninstall  Unload and remove the launch agent
  start      Trigger the headless startup script once
  stop       Stop the SearXNG stack and Colima profile
  restart    Stop then start the stack again
  status     Show Colima, Docker context, Compose, and endpoint status
  logs       Tail the headless runtime logs
EOF
}

ensure_logs() {
  mkdir -p "${ROOT_DIR}/.local/logs"
  touch "${STDOUT_LOG}" "${STDERR_LOG}"
}

render_plist() {
  local escaped_root="${ROOT_DIR//\\/\\\\}"
  escaped_root="${escaped_root//&/\\&}"
  escaped_root="${escaped_root//|/\\|}"
  sed "s|__KBA_REPO_ROOT__|${escaped_root}|g" "${PLIST_SOURCE}" > "${PLIST_TARGET}"
}

install_agent() {
  ensure_logs
  mkdir -p "${HOME}/Library/LaunchAgents"
  render_plist
  launchctl bootout "${GUI_DOMAIN}" "${PLIST_TARGET}" >/dev/null 2>&1 || true
  launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"
  launchctl enable "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
  echo "Installed ${LABEL}"
}

uninstall_agent() {
  launchctl bootout "${GUI_DOMAIN}" "${PLIST_TARGET}" >/dev/null 2>&1 || true
  rm -f "${PLIST_TARGET}"
  echo "Removed ${LABEL}"
}

start_stack() {
  ensure_logs
  "${RUN_SCRIPT}"
}

stop_stack() {
  "${STOP_SCRIPT}"
}

restart_stack() {
  stop_stack
  start_stack
}

status_stack() {
  echo "launchd:"
  launchctl print "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || echo "  not loaded"
  echo
  echo "colima:"
  if command -v colima >/dev/null 2>&1; then
    colima status --profile "${PROFILE}" || echo "  not running"
  else
    echo "  colima not installed"
  fi
  echo
  echo "docker context:"
  if command -v docker >/dev/null 2>&1; then
    echo "  ${DOCKER_CONTEXT}"
    echo
    echo "compose:"
    if docker context ls --format '{{.Name}}' | grep -qx "${DOCKER_CONTEXT}"; then
      SEARXNG_PORT="${PORT}" SEARXNG_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
        docker --context "${DOCKER_CONTEXT}" compose -p "${COMPOSE_PROJECT}" -f "${ROOT_DIR}/infra/searxng/docker-compose.yml" ps 2>/dev/null || echo "  stack not running"
    else
      echo "  docker context ${DOCKER_CONTEXT} not found"
    fi
  else
    echo "  docker not installed"
  fi
  echo
  echo "endpoint:"
  curl -sS "http://127.0.0.1:${PORT}/search?q=searxng&format=json" >/dev/null && echo "  http://127.0.0.1:${PORT} reachable" || echo "  http://127.0.0.1:${PORT} unreachable"
}

logs_stack() {
  ensure_logs
  tail -n 40 -f "${STDOUT_LOG}" "${STDERR_LOG}"
}

case "${1:-}" in
  install) install_agent ;;
  uninstall) uninstall_agent ;;
  start) start_stack ;;
  stop) stop_stack ;;
  restart) restart_stack ;;
  status) status_stack ;;
  logs) logs_stack ;;
  *) usage; exit 1 ;;
esac
