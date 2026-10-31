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
  all) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh" "$SCRIPT_DIR/conversations.sh" "$SCRIPT_DIR/otel_smoke.sh") ;;
  artifacts) CASE_SCRIPTS=("$SCRIPT_DIR/artifacts.sh") ;;
  conversations) CASE_SCRIPTS=("$SCRIPT_DIR/conversations.sh") ;;
  otel-smoke) CASE_SCRIPTS=("$SCRIPT_DIR/otel_smoke.sh") ;;
  *)
    echo "usage: tests/e2e/run.sh [all|artifacts|conversations|otel-smoke]" >&2
    exit 2
    ;;
esac

# Isolate autonomous skill evolution for this suite and every child/restart.
source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"
e2e_environment default "$0" "$@"

(cd "$REPO_ROOT/docker" && ./restart.sh)
# 通知 conversations.sh 跳过重复重建（inspect 使用重建后的最新 Mounts）
export N_AGENT_E2E_REBUILT=1
CHECK_KEYS=()
for case_script in "${CASE_SCRIPTS[@]}"; do
  case "$(basename "$case_script")" in
    artifacts.sh) CHECK_KEYS+=(artifacts) ;;
  esac
done
if [ "${#CHECK_KEYS[@]}" -gt 0 ]; then
  CHECK_RUN_ID="management-checks-$(date +%s)-$$"
  CHECK_REPORT_DIR="$REPO_ROOT/locals/e2e-reports/$CHECK_RUN_ID"
  python3 "$SCRIPT_DIR/result_report.py" "$CHECK_REPORT_DIR" init-checks "$CHECK_RUN_ID" "${CHECK_KEYS[@]}"
fi
RUN_ID="e2e-$(date +%s)-$$"
REPORT_DIR="$REPO_ROOT/locals/e2e-reports/$RUN_ID"
mkdir -p "$REPORT_DIR"
REPORT_DIRS=()
[ "${#CHECK_KEYS[@]}" -eq 0 ] || REPORT_DIRS+=("$CHECK_REPORT_DIR")
result=0
for case_script in "${CASE_SCRIPTS[@]}"; do
  case_result=0
  "$case_script" 2>&1 | tee "$REPORT_DIR/$(basename "$case_script").log" || case_result=$?
  if [ "$(basename "$case_script")" = artifacts.sh ]; then
    verdict=PASS
    [ "$case_result" -eq 0 ] || verdict=FAIL
    python3 "$SCRIPT_DIR/result_report.py" "$CHECK_REPORT_DIR" check artifacts "$verdict"
    python3 "$SCRIPT_DIR/result_report.py" "$CHECK_REPORT_DIR" finish "" "$case_result"
  else
    found_report=0
    while IFS= read -r child_dir; do
      # Conversation reports originate inside Docker; map their run directory to the host mount.
      child_report="$REPO_ROOT/locals/e2e-reports/$(basename "$child_dir")"
      if [ -f "$child_report/report.json" ]; then
        REPORT_DIRS+=("$child_report")
        found_report=1
      fi
    done < <(sed -n 's/^REPORT_DIR //p' "$REPORT_DIR/$(basename "$case_script").log")
    category=对话
    [ "$(basename "$case_script")" != otel_smoke.sh ] || category=OTel
    if [ "$found_report" -eq 1 ] && [ "$case_result" -ne 0 ]; then
      failure_dir="$REPORT_DIR/$(basename "$case_script" .sh)-failure"
      python3 "$SCRIPT_DIR/result_report.py" "$failure_dir" suite-failure "$RUN_ID" "$category" "$(basename "$case_script") / 套件执行或清理" "$case_result"
      REPORT_DIRS+=("$failure_dir")
    fi
    if [ "$found_report" -eq 0 ]; then
      [ "$case_result" -ne 0 ] || case_result=1
      missing_dir="$REPORT_DIR/$(basename "$case_script" .sh)-missing"
      python3 "$SCRIPT_DIR/result_report.py" "$missing_dir" missing "$RUN_ID" "$category" "$(basename "$case_script")" "$case_result"
      REPORT_DIRS+=("$missing_dir")
    fi
  fi
  if [ "$case_result" -ne 0 ]; then
    result="$case_result"
    break
  fi
done
python3 "$SCRIPT_DIR/result_report.py" "$REPORT_DIR" aggregate "$RUN_ID" "$result" "${REPORT_DIRS[@]}"
exit "$result"
