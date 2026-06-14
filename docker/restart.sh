#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# restart
docker compose stop n-agent
docker compose rm -f n-agent
docker compose up -d --build --force-recreate n-agent
echo

# status
sleep 2
echo "compose ps n-kb"
docker compose ps n-agent
echo

sleep 2
echo "curl -fsS http://nagent.localhost/health"
curl -fsS http://nagent.localhost/health | jq .
