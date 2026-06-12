#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose stop n-agent
docker compose rm -f n-agent
docker compose up -d --build --force-recreate n-agent

docker compose ps n-agent
