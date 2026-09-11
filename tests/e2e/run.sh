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
  all) CASE_SCRIPTS=("$SCRIPT_DIR/task.sh" "$SCRIPT_DIR/artifacts.sh" "$SCRIPT_DIR/config-bundle.sh") ;;
  task) CASE_SCRIPTS=("$SCRIPT_DIR/task.sh") ;;
  artifacts) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh") ;;
  config-bundle) CASE_SCRIPTS=("$SCRIPT_DIR/config-bundle.sh") ;;
  *)
    echo "usage: tests/e2e/run.sh [all|task|artifacts|config-bundle]" >&2
    exit 2
    ;;
esac

(cd "$REPO_ROOT/docker" && ./restart.sh)
for case_script in "${CASE_SCRIPTS[@]}"; do
  "$case_script"
done
