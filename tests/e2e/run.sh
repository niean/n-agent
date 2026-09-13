#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SUITE="${1:-all}"

if ! command -v node >/dev/null 2>&1; then
  echo "E2E requires host Node.js to validate CLI JSON output" >&2
  exit 2
fi

case "$SUITE" in
  all) CASE_SCRIPTS=("$SCRIPT_DIR/task.sh" "$SCRIPT_DIR/artifacts.sh" "$SCRIPT_DIR/config-bundle.sh" "$SCRIPT_DIR/conversations.sh") ;;
  task) CASE_SCRIPTS=("$SCRIPT_DIR/task.sh") ;;
  artifacts) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh") ;;
  config-bundle) CASE_SCRIPTS=("$SCRIPT_DIR/config-bundle.sh") ;;
  conversations) CASE_SCRIPTS=("$SCRIPT_DIR/conversations.sh") ;;
  *)
    echo "usage: tests/e2e/run.sh [all|task|artifacts|config-bundle|conversations]" >&2
    exit 2
    ;;
esac

(cd "$REPO_ROOT/docker" && ./restart.sh)
# 通知 conversations.sh 跳过重复重建（inspect 使用重建后的最新 Mounts）
export N_AGENT_E2E_REBUILT=1
for case_script in "${CASE_SCRIPTS[@]}"; do
  "$case_script"
done
