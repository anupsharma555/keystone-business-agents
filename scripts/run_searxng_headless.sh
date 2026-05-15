#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${KBA_REPO_ROOT:-${SCRIPT_DIR:h}}"
COMPOSE_FILE="${ROOT_DIR}/infra/searxng/docker-compose.yml"
LOG_DIR="${ROOT_DIR}/.local/logs"
COLIMA_BIN="${COLIMA_BIN:-$(command -v colima || true)}"
DOCKER_BIN="${DOCKER_BIN:-$(command -v docker || true)}"
PROFILE="${SEARXNG_COLIMA_PROFILE:-kba-searxng}"
PORT="${SEARXNG_PORT:-18080}"
COMPOSE_PROJECT="${SEARXNG_COMPOSE_PROJECT:-kba-searxng}"
MOUNT_SPEC="${SEARXNG_COLIMA_MOUNT:-${ROOT_DIR}:w}"
MOUNT_INOTIFY="${SEARXNG_COLIMA_MOUNT_INOTIFY:-false}"
DOCKER_CONTEXT="${SEARXNG_DOCKER_CONTEXT:-colima-${PROFILE}}"
if [[ "${PROFILE}" == "default" && -z "${SEARXNG_DOCKER_CONTEXT:-}" ]]; then
  DOCKER_CONTEXT="colima"
fi

mkdir -p "${LOG_DIR}"

require_bin() {
  local name="$1"
  local path_value="$2"
  if [[ -z "${path_value}" ]]; then
    echo "Missing required binary: ${name}" >&2
    exit 1
  fi
}

require_bin "colima" "${COLIMA_BIN}"
require_bin "docker" "${DOCKER_BIN}"

echo "Ensuring Colima profile '${PROFILE}' is running..."
if ! "${COLIMA_BIN}" status --profile "${PROFILE}" >/dev/null 2>&1; then
  "${COLIMA_BIN}" start \
    --profile "${PROFILE}" \
    --runtime docker \
    --mount "${MOUNT_SPEC}" \
    --mount-inotify="${MOUNT_INOTIFY}"
fi

DOCKER_ARGS=()
if "${DOCKER_BIN}" context ls --format '{{.Name}}' | grep -qx "${DOCKER_CONTEXT}"; then
  DOCKER_ARGS=(--context "${DOCKER_CONTEXT}")
else
  echo "Docker context '${DOCKER_CONTEXT}' was not found after starting Colima profile '${PROFILE}'." >&2
  exit 1
fi

echo "Waiting for the Docker daemon..."
for _ in {1..30}; do
  if "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" info >/dev/null 2>&1; then
  echo "Docker daemon did not become ready under Colima." >&2
  exit 1
fi

echo "Starting SearXNG stack on http://127.0.0.1:${PORT}..."
SEARXNG_PORT="${PORT}" SEARXNG_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
  "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d
SEARXNG_PORT="${PORT}" SEARXNG_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
  "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" ps

echo "Waiting for SearXNG endpoint..."
for _ in {1..45}; do
  if curl -fsS "http://127.0.0.1:${PORT}/search?q=searxng%20health%20check&format=json" >/dev/null 2>&1; then
    echo "SearXNG endpoint is reachable."
    echo "SearXNG headless startup complete."
    exit 0
  fi
  sleep 2
done

echo "SearXNG endpoint did not become ready at http://127.0.0.1:${PORT}." >&2
exit 1
