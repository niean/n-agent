#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose stop qdrant
docker compose up -d --force-recreate qdrant

docker compose ps qdrant

curl -fsS http://localhost:6333/ | jq .
