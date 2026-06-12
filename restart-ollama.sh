#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose stop ollama
docker compose up -d --force-recreate ollama

docker compose ps ollama

curl -fsS http://localhost:11434/api/tags | jq .
