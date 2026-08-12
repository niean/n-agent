#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'codex provider: expected canonical repository root' >&2
    exit 64
fi

repo_root=$1
codex_bin=$(command -v codex 2>/dev/null || true)
if [ -z "$codex_bin" ]; then
    printf '%s\n' 'codex provider: codex command is unavailable' >&2
    exit 127
fi

set -- exec --cd "$repo_root" --sandbox workspace-write
if [ -n "${HARNESS_THIRD_REVIEW_MODEL:-}" ]; then
    set -- "$@" --model "$HARNESS_THIRD_REVIEW_MODEL"
fi
set -- "$@" -

deadline=${HARNESS_THIRD_REVIEW_CODEX_TIMEOUT_SECONDS:-}
if [ -z "$deadline" ]; then
    exec "$codex_bin" "$@"
fi
case $deadline in
    *[!0-9]*|'') printf '%s\n' 'codex provider: invalid timeout' >&2; exit 64 ;;
esac
if [ "$deadline" -lt 1 ] || [ "$deadline" -gt 900 ]; then
    printf '%s\n' 'codex provider: timeout must be between 1 and 900 seconds' >&2
    exit 64
fi

"$codex_bin" "$@" &
child_pid=$!
(
    sleep "$deadline"
    kill -TERM "$child_pid" 2>/dev/null || exit 0
    sleep 2
    kill -KILL "$child_pid" 2>/dev/null || true
) &
watchdog_pid=$!

forward_signal() {
    kill -TERM "$child_pid" 2>/dev/null || true
}
trap forward_signal HUP INT TERM

set +e
wait "$child_pid"
child_status=$?
set -e
kill "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true
exit "$child_status"
