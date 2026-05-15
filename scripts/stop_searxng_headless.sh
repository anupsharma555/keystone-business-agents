#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${KBA_REPO_ROOT:-${SCRIPT_DIR:h}}"
COMPOSE_FILE="${ROOT_DIR}/infra/searxng/docker-compose.yml"
COLIMA_BIN="${COLIMA_BIN:-$(command -v colima || true)}"
DOCKER_BIN="${DOCKER_BIN:-$(command -v docker || true)}"
PROFILE="${SEARXNG_COLIMA_PROFILE:-kba-searxng}"
PORT="${SEARXNG_PORT:-18080}"
COMPOSE_PROJECT="${SEARXNG_COMPOSE_PROJECT:-kba-searxng}"
DOCKER_CONTEXT="${SEARXNG_DOCKER_CONTEXT:-colima-${PROFILE}}"
if [[ "${PROFILE}" == "default" && -z "${SEARXNG_DOCKER_CONTEXT:-}" ]]; then
  DOCKER_CONTEXT="colima"
fi

if [[ -n "${DOCKER_BIN}" ]]; then
  DOCKER_ARGS=()
  if "${DOCKER_BIN}" context ls --format '{{.Name}}' | grep -qx "${DOCKER_CONTEXT}"; then
    DOCKER_ARGS=(--context "${DOCKER_CONTEXT}")
  else
    echo "Docker context '${DOCKER_CONTEXT}' not found; skipping compose shutdown."
    DOCKER_ARGS=()
  fi
  if [[ ${#DOCKER_ARGS[@]} -gt 0 ]] && "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" info >/dev/null 2>&1; then
    SEARXNG_PORT="${PORT}" SEARXNG_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
      "${DOCKER_BIN}" "${DOCKER_ARGS[@]}" compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" down || true
  fi
fi

if [[ -n "${COLIMA_BIN}" ]]; then
  "${COLIMA_BIN}" stop --profile "${PROFILE}" >/dev/null 2>&1 || true
fi

echo "Stopped SearXNG headless runtime."
