#!/usr/bin/env bash
# This script needs bash (arrays, ${BASH_SOURCE}); it is also invoked as
# `sh tests/e2e/run.sh`, so re-exec under bash before any bash-only syntax
# is parsed (same guard pattern as docker/restart.sh).
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

# Environment note (observability smoke, plan item 23): COMPOSE_FILE /
# COMPOSE_PROFILES / N_AGENT_COMPOSE_STOP_TIMEOUT are inherited untouched,
# because the restart below re-evaluates them. Observability acceptance
# runs with COMPOSE_PROFILES=observability (and, for the Profile branch,
# COMPOSE_FILE merging docker/docker-compose.observability.yml) plus the
# N_AGENT_OTEL_* / N_AGENT_PROFILING_* values in docker/.env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SUITE="${1:-all}"

if ! command -v node >/dev/null 2>&1; then
  echo "E2E requires host Node.js to validate CLI JSON output" >&2
  exit 2
fi

case "$SUITE" in
  all) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh" "$SCRIPT_DIR/config-bundle.sh" "$SCRIPT_DIR/conversations.sh" "$SCRIPT_DIR/otel_smoke.sh") ;;
  artifacts) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh") ;;
  config-bundle) CASE_SCRIPTS=("$SCRIPT_DIR/config-bundle.sh") ;;
  conversations) CASE_SCRIPTS=("$SCRIPT_DIR/conversations.sh") ;;
  otel-smoke) CASE_SCRIPTS=("$SCRIPT_DIR/otel_smoke.sh") ;;
  *)
    echo "usage: tests/e2e/run.sh [all|artifacts|config-bundle|conversations|otel-smoke]" >&2
    exit 2
    ;;
esac

# Isolate autonomous skill evolution for this suite and every child/restart.
source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"
e2e_environment default "$0" "$@"

(cd "$REPO_ROOT/docker" && ./restart.sh)
# 通知 conversations.sh 跳过重复重建（inspect 使用重建后的最新 Mounts）
export N_AGENT_E2E_REBUILT=1
for case_script in "${CASE_SCRIPTS[@]}"; do
  "$case_script"
done
