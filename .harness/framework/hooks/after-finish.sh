#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
DOCKER_DIR="$REPO_ROOT/docker"

echo "[after-finish] 重启服务: docker/restart.sh"
cd "$DOCKER_DIR" && exec bash ./restart.sh
