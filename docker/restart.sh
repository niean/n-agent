#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST_HEALTH_URL="${N_AGENT_HOST_HEALTH_URL:-http://127.0.0.1:8201/health}"
PUBLIC_HEALTH_URL="${N_AGENT_PUBLIC_HEALTH_URL:-http://nagent.localhost/health}"
COMPOSE_STOP_TIMEOUT="${N_AGENT_COMPOSE_STOP_TIMEOUT:-1}"
CONTAINER_HEALTH_ATTEMPTS="${N_AGENT_CONTAINER_HEALTH_ATTEMPTS:-10}"
HOST_HEALTH_ATTEMPTS="${N_AGENT_HOST_HEALTH_ATTEMPTS:-6}"
PUBLIC_HEALTH_ATTEMPTS="${N_AGENT_PUBLIC_HEALTH_ATTEMPTS:-6}"
HTTP_WAIT_TIMEOUT_SECONDS="${N_AGENT_HTTP_WAIT_TIMEOUT_SECONDS:-1}"
HTTP_FINAL_TIMEOUT_SECONDS="${N_AGENT_HTTP_FINAL_TIMEOUT_SECONDS:-5}"
CURL_WAIT_ARGS=(--connect-timeout "$HTTP_WAIT_TIMEOUT_SECONDS" --max-time "$HTTP_WAIT_TIMEOUT_SECONDS")
CURL_FINAL_ARGS=(--connect-timeout "$HTTP_FINAL_TIMEOUT_SECONDS" --max-time "$HTTP_FINAL_TIMEOUT_SECONDS")

container_health() {
  docker compose exec -T n-agent python - <<'PY' >/dev/null 2>&1
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8201/health", timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"unexpected status: {response.status}")
PY
}

host_health() {
  curl -fsS "${CURL_WAIT_ARGS[@]}" "$1" >/dev/null 2>&1
}

wait_until() {
  local label="$1"
  local attempts="$2"
  shift 2

  for attempt in $(seq 1 "$attempts"); do
    if "$@"; then
      echo "$label ready"
      return 0
    fi
    echo "$label waiting ($attempt/$attempts)"
    sleep 1
  done

  echo "$label did not become ready after $attempts attempts" >&2
  return 1
}

recover_stale_port_proxy() {
  if host_health "$HOST_HEALTH_URL"; then
    return 0
  fi

  if ! container_health; then
    echo "container health failed; Docker port proxy is not the first suspect" >&2
    return 1
  fi

  echo "container is healthy but host port is not responding; restarting service to refresh Docker Desktop port proxy"
  docker compose restart --timeout "$COMPOSE_STOP_TIMEOUT" n-agent
  wait_until "container health" "$CONTAINER_HEALTH_ATTEMPTS" container_health
  wait_until "host port health" "$HOST_HEALTH_ATTEMPTS" host_health "$HOST_HEALTH_URL"
}

# restart (bring up all services incl. browser; n-agent depends_on browser healthy)
docker compose down --timeout "$COMPOSE_STOP_TIMEOUT"
# Compose can occasionally leave a stopped service container behind after
# down. Remove any such containers before up reuses their generated names.
docker compose rm --force --stop
docker compose up -d --build --force-recreate --remove-orphans
echo

# status
sleep 2
echo "compose ps"
docker compose ps
echo

# health
wait_until "container health" "$CONTAINER_HEALTH_ATTEMPTS" container_health
recover_stale_port_proxy
wait_until "public health" "$PUBLIC_HEALTH_ATTEMPTS" host_health "$PUBLIC_HEALTH_URL"

echo "curl -fsS ${PUBLIC_HEALTH_URL}"
if command -v jq >/dev/null 2>&1; then
  curl -fsS "${CURL_FINAL_ARGS[@]}" "$PUBLIC_HEALTH_URL" | jq .
else
  curl -fsS "${CURL_FINAL_ARGS[@]}" "$PUBLIC_HEALTH_URL"
  echo
fi
