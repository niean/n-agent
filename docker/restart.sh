#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST_HEALTH_URL="${N_AGENT_HOST_HEALTH_URL:-http://127.0.0.1:8201/health}"
PUBLIC_HEALTH_URL="${N_AGENT_PUBLIC_HEALTH_URL:-http://nagent.localhost/health}"
COMPOSE_STOP_TIMEOUT="${N_AGENT_COMPOSE_STOP_TIMEOUT:-1}"
CURL_TIMEOUT_ARGS=(--connect-timeout 2 --max-time 5)

container_health() {
  docker compose exec -T n-agent python - <<'PY' >/dev/null 2>&1
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8201/health", timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"unexpected status: {response.status}")
PY
}

host_health() {
  curl -fsS "${CURL_TIMEOUT_ARGS[@]}" "$1" >/dev/null 2>&1
}

wait_until() {
  local label="$1"
  shift

  for attempt in $(seq 1 30); do
    if "$@"; then
      echo "$label ready"
      return 0
    fi
    sleep 1
  done

  echo "$label did not become ready in 30s" >&2
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
  wait_until "container health" container_health
  wait_until "host port health" host_health "$HOST_HEALTH_URL"
}

# restart
docker compose down --timeout "$COMPOSE_STOP_TIMEOUT" n-agent
docker compose rm -f n-agent
docker compose up -d --build --force-recreate --remove-orphans n-agent
echo

# status
sleep 2
echo "compose ps n-agent"
docker compose ps n-agent
echo

# health
wait_until "container health" container_health
recover_stale_port_proxy
wait_until "public health" host_health "$PUBLIC_HEALTH_URL"

echo "curl -fsS ${PUBLIC_HEALTH_URL}"
if command -v jq >/dev/null 2>&1; then
  curl -fsS "${CURL_TIMEOUT_ARGS[@]}" "$PUBLIC_HEALTH_URL" | jq .
else
  curl -fsS "${CURL_TIMEOUT_ARGS[@]}" "$PUBLIC_HEALTH_URL"
  echo
fi
