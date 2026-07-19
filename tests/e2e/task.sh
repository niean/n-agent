#!/usr/bin/env bash

(
  set -eu
  set -o pipefail

  task_id=""
  title="n-agent-e2e-task-$(date +%s)-$$"
  body="created by Task CLI E2E"

  cleanup() {
    if [ -n "$task_id" ]; then
      docker exec n-agent-n-agent-1 n-agent task delete "$task_id" --json >/dev/null 2>&1 || true
    fi
  }

  cleanup_and_exit() {
    exit_code="$1"
    trap - EXIT HUP INT TERM
    cleanup
    exit "$exit_code"
  }

  trap cleanup EXIT
  trap 'cleanup_and_exit 129' HUP
  trap 'cleanup_and_exit 130' INT
  trap 'cleanup_and_exit 143' TERM

  normalize_json() {
    printf '%s\n' "$1" | tr -d '\r'
  }

  assert_valid_json() {
    normalize_json "$1" |
      node -e 'JSON.parse(require("fs").readFileSync(0, "utf8"))'
  }

  created_task_id() {
    normalized="$(normalize_json "$1")"
    assert_valid_json "$normalized" || return 1
    [ "$(printf '%s\n' "$normalized" | sed -n '1p')" = "{" ] || return 1
    [ "$(printf '%s\n' "$normalized" | sed -n '$p')" = "}" ] || return 1

    id_line="$(printf '%s\n' "$normalized" | sed -n '2p')"
    id="$(
      printf '%s\n' "$id_line" |
        sed -n 's/^  "id": "\(t_[0-9a-f]\{16\}\)",$/\1/p'
    )"
    [ -n "$id" ] || return 1
    [ "$(printf '%s\n' "$normalized" | grep -Ec '^  "id": "t_[0-9a-f]{16}",$')" -eq 1 ] || return 1
    printf '%s\n' "$id"
  }

  assert_contains() {
    normalize_json "$1" | grep -F -- "$2" >/dev/null || {
      echo "FAIL: expected output to contain: $2" >&2
      return 1
    }
  }

  assert_clean_json() {
    normalized="$(normalize_json "$1")"
    assert_valid_json "$normalized" || return 1
    case "$2" in
      object)
        [ "$(printf '%s\n' "$normalized" | sed -n '1p')" = "{" ] || return 1
        [ "$(printf '%s\n' "$normalized" | sed -n '$p')" = "}" ] || return 1
        ;;
      array)
        [ "$(printf '%s\n' "$normalized" | sed -n '1p')" = "[" ] || return 1
        [ "$(printf '%s\n' "$normalized" | sed -n '$p')" = "]" ] || return 1
        ;;
      *) return 2 ;;
    esac
  }

  echo "[Task E2E] create"
  create_json="$(docker exec n-agent-n-agent-1 n-agent task create --title "$title" --body "$body" --created-by e2e --json)"
  if ! task_id="$(created_task_id "$create_json")"; then
    echo "FAIL: create response is not a clean Task JSON object" >&2
    task_id=""
    exit 1
  fi

  echo "[Task E2E] show"
  show_json="$(docker exec n-agent-n-agent-1 n-agent task show "$task_id" --json)"
  assert_clean_json "$show_json" object
  assert_contains "$show_json" "\"id\": \"$task_id\""
  assert_contains "$show_json" "\"title\": \"$title\""
  assert_contains "$show_json" "\"body\": \"$body\""
  assert_contains "$show_json" '"status": "triage"'

  echo "[Task E2E] list --all"
  list_json="$(docker exec n-agent-n-agent-1 n-agent task list --all --json)"
  assert_clean_json "$list_json" array
  assert_contains "$list_json" "\"id\": \"$task_id\""
  assert_contains "$list_json" "\"title\": \"$title\""

  echo "[Task E2E] delete"
  delete_json="$(docker exec n-agent-n-agent-1 n-agent task delete "$task_id" --json)"
  assert_clean_json "$delete_json" object
  [ "$(normalize_json "$delete_json" | wc -l | tr -d ' ')" -eq 4 ]
  [ "$(normalize_json "$delete_json" | sed -n '2p')" = "  \"id\": \"$task_id\"," ]
  [ "$(normalize_json "$delete_json" | sed -n '3p')" = '  "deleted": true' ]

  echo "[Task E2E] confirm deleted"
  set +e
  missing_output="$(docker exec n-agent-n-agent-1 n-agent task show "$task_id" --json 2>&1)"
  missing_rc=$?
  set -e
  if [ "$missing_rc" -ne 1 ] ||
     [ "$(normalize_json "$missing_output")" != "error: task not found: $task_id" ]; then
    echo "FAIL: deleted task check returned unexpected result: rc=$missing_rc" >&2
    exit 1
  fi

  task_id=""
  trap - EXIT HUP INT TERM
  echo "[Task E2E] PASS"
)
